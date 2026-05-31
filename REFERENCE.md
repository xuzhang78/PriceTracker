# PriceTracker — Developer Reference

**Project:** B&H Photo MacBook Pro M5 Price Monitor
**Repo:** [github.com/xuzhang78/PriceTracker](https://github.com/xuzhang78/PriceTracker)
**Last updated:** 2026-05-31

This document is the single source of truth for understanding, operating, and modifying this project. It covers the original design decisions, every post-launch fix, and the hard-won SDK-specific knowledge that isn't in the official docs.

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [File Structure](#2-file-structure)
3. [Architecture & Data Flow](#3-architecture--data-flow)
4. [SGAI SDK — Critical Implementation Details](#4-sgai-sdk--critical-implementation-details)
5. [Credit Usage — Actual vs. Estimated](#5-credit-usage--actual-vs-estimated)
6. [Post-Launch Fixes (Chronological)](#6-post-launch-fixes-chronological)
7. [Product Management](#7-product-management)
8. [GitHub Actions Setup](#8-github-actions-setup)
9. [ntfy.sh Setup](#9-ntfysh-setup)
10. [Local Testing](#10-local-testing)
11. [Cost Management](#11-cost-management)
12. [Scaling to Other Products & Sites](#12-scaling-to-other-products--sites)
13. [Troubleshooting](#13-troubleshooting)

---

## 1. System Overview

Checks the price of tracked products daily using AI extraction and sends a push notification when price drops below a configured threshold.

**Stack:**
- **Scheduler:** GitHub Actions cron (`0 9 * * *` → 09:00 UTC daily)
- **AI Extraction:** ScrapeGraphAI cloud `extract()` API
- **Notifications:** ntfy.sh push (primary), Telegram (commented-out alternative)
- **Config:** `products.json` — add/pause/remove products with no code changes

**Why GitHub Actions instead of SGAI Monitors:**
SGAI's Monitor feature sends data via webhook, which requires a public-URL server to receive it. GitHub Actions acts as the scheduler instead, calling `sgai.extract()` fresh on each run. This eliminates the need for any hosted server.

**Why `sgai.extract()` instead of `sgai.monitor.create()`:**
`monitor.create()` uses `schedule=` (not `interval=`), returns `monitor.data.cron_id` (not `monitor['id']`), and has no `prompt=` parameter for alert logic. The alert threshold logic must live in external code regardless. GitHub Actions + `extract()` is simpler end-to-end.

---

## 2. File Structure

```
PriceTracker/
├── .env                          ← local secrets (gitignored — NEVER commit)
├── .gitignore
├── requirements.txt
├── products.json                 ← product list; set active:false to pause
├── check_prices.py               ← core logic: extract → validate → notify
├── .github/
│   └── workflows/
│       └── price_check.yml       ← daily cron + manual trigger
└── README.md                     ← end-user setup guide
```

### File purposes

| File | Change when... |
|---|---|
| `products.json` | Adding/pausing/removing tracked products |
| `check_prices.py` | Changing price threshold logic, retry behavior, notification format, or field extraction |
| `price_check.yml` | Changing schedule, Python version, or adding new env vars |
| `.env` | Updating API keys locally (never push) |

---

## 3. Architecture & Data Flow

```
GitHub Actions (cron: 0 9 * * *)
    └── runs check_prices.py
            ├── loads products.json → filters active: true entries
            └── for each active product:
                    ├── sgai.extract(url, schema_dict, prompt, FetchConfig)
                    │       └── ScrapeGraphAI cloud: renders page (JS + stealth)
                    │                               → AI extracts structured data
                    │                               → returns ExtractResponse
                    ├── reads result.data.json_data["current_price"] etc.
                    ├── sanity guard: 100 < price < 10,000
                    ├── if price < threshold → POST to ntfy.sh
                    └── if error at any step → POST warning to ntfy.sh
```

---

## 4. SGAI SDK — Critical Implementation Details

> [!IMPORTANT]
> This section documents hard-won knowledge from debugging. Read before modifying `check_prices.py`.

### Python Version Requirement

`scrapegraph-py` v2.x requires **Python 3.12+**, NOT 3.11 or earlier.

```
# From pip error:
ERROR: 2.0.1 Requires-Python >=3.12
ERROR: 2.1.0 Requires-Python >=3.12
```

The GitHub Actions workflow uses `python-version: "3.12"` for this reason.

### `sgai.extract()` — Correct Call Signature

```python
result = sgai.extract(
    url=url,
    schema=MacBookProductSchema.model_json_schema(),  # ← MUST be a dict, not the class
    prompt="Extract the product title, current retail price as a float, and stock status.",
    fetch_config=FetchConfig(mode="js", stealth=True, wait=2000),
)
```

**Critical:** `schema=` expects a **JSON schema dict**, not a Pydantic class.
- ❌ `schema=MacBookProductSchema` → `ValidationError: Input should be a valid dictionary`
- ✅ `schema=MacBookProductSchema.model_json_schema()` → works

### `ExtractResponse` Object Structure

`sgai.extract()` returns an `ApiResult`. The actual extracted data lives at:

```python
result.data           # ExtractResponse object (NOT a dict, NOT the schema fields directly)
result.data.json_data # dict | None  ← THIS is where extracted fields live (aliased from "json")
result.data.raw       # str | None   ← raw text response (useful for debugging empty json_data)
result.data.usage     # TokenUsage
result.data.metadata  # dict
```

**Full `ExtractResponse` definition** (from `scrapegraph_py/schemas.py`):
```python
class ExtractResponse(ResponseModel):
    raw: str | None
    json_data: dict | None = Field(default=None, alias="json")
    usage: TokenUsage
    metadata: dict
    model_config = ConfigDict(extra="allow", populate_by_name=True)
```

**Correct field access pattern:**
```python
json_data = result.data.json_data          # the dict
price     = float(json_data["current_price"])
title     = str(json_data["product_title"])
stock     = str(json_data["stock_status"])
```

**Wrong patterns (all have been tried and failed):**
```python
result.data.current_price          # ❌ AttributeError: ExtractResponse has no attr 'current_price'
MacBookProductSchema(**result.data) # ❌ TypeError: argument after ** must be a mapping, not ExtractResponse
result.data["current_price"]       # ❌ TypeError: ExtractResponse is not subscriptable
```

### `FetchConfig` for Anti-Bot Sites

```python
from scrapegraph_py import FetchConfig
FetchConfig(mode="js", stealth=True, wait=2000)
```

- `mode="js"`: JavaScript rendering (required for React/SPA sites, and B&H Photo)
- `stealth=True`: Cloudflare/bot-detection bypass (+5 credits per call)
- `wait=2000`: 2-second wait for dynamic content to load (milliseconds)

### Retry Logic for 504 Gateway Timeouts

504s are transient and cost **0 credits** (request fails before SGAI processes it). The script retries up to 3 times:

```python
RETRY_DELAYS = [15, 30, 60]  # seconds between attempts
```

Only fires an error notification if all 3 attempts fail. Total worst-case wait: ~105 seconds per product.

---

## 5. Credit Usage — Actual vs. Estimated

| | Estimated (pre-launch) | **Actual (confirmed)** |
|---|---|---|
| Cost per `extract()` call | 5–8 credits | **10 credits** |
| 2 products/day × 31 days | 310–496/mo | **620/mo** |
| Free plan (500/mo) sufficient? | ✅ Yes | ❌ No (120 over) |
| 1 product/day × 31 days | — | **310/mo** ✅ |

**Free plan limits:**
- 500 credits/month (recurring)
- 500 one-time bonus credits
- 10 requests/min (not relevant at 1–2 calls/day)
- 1 monitor, 1 concurrent crawl (not used — we use `extract()`, not monitors)

**Credit breakdown per call:** 5 base + 5 stealth mode = **10 credits**

504 errors cost **0 credits** (confirmed via dashboard).

---

## 6. Post-Launch Fixes (Chronological)

### Fix 1 — Node.js 20 Deprecation Warning

**Symptom:** GitHub Actions annotation: `actions/checkout@v4, actions/setup-python@v5 are running on Node.js 20`

**Root cause:** Actions v4/v5 bundle Node.js 20 runtime, which GitHub is deprecating.

**Fix:** Upgrade to versions that natively target Node.js 24:
```yaml
# Before
uses: actions/checkout@v4
uses: actions/setup-python@v5

# After
uses: actions/checkout@v5
uses: actions/setup-python@v6
```

Also removed the `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: true` env var added as a temporary workaround.

---

### Fix 2 — SDK Schema Validation Error

**Symptom (ntfy notification):**
```
Reason: 1 validation error for ExtractRequest
schema
  Input should be a valid dictionary [type=dict_type,
  input_value=<class '__main__.MacBookProductSchema'>, input_type=ModelMetaclass]
```

**Root cause:** `sgai.extract(schema=MacBookProductSchema)` passes the Pydantic class itself. The SDK's `ExtractRequest` model validates that `schema` is a dict.

**Fix:**
```python
# Before
schema=MacBookProductSchema

# After
schema=MacBookProductSchema.model_json_schema()
```

---

### Fix 3 — Wrong Result Data Access

**Symptom (ntfy notification):**
```
Reason: Parse error: __main__.MacBookProductSchema() argument after **
must be a mapping, not ExtractResponse
```

**Root cause:** After Fix 2, we tried `MacBookProductSchema(**result.data)` to unpack the result. But `result.data` is an `ExtractResponse` object, not a dict.

**Attempted wrong fix:**
```python
MacBookProductSchema(**result.data)  # ❌ fails
```

**Symptom after wrong fix:**
```
Reason: Field access error: 'ExtractResponse' object has no attribute 'current_price'
```

**Root cause:** `ExtractResponse` doesn't expose schema fields as direct attributes. They live inside `.json_data`.

**Correct fix** (discovered by inspecting `scrapegraph_py/schemas.py`):
```python
json_data = result.data.json_data  # ← the actual extracted dict
price = float(json_data["current_price"])
```

---

### Fix 4 — HTTP 504 Gateway Timeout

**Symptom (ntfy notification):** `Reason: http 504`

**Root cause:** Transient SGAI server timeout when scraping B&H Photo. Confirmed free (0 credits deducted per dashboard).

**Fix:** Retry loop with exponential backoff:
```python
MAX_RETRIES = 3
RETRY_DELAYS = [15, 30, 60]  # seconds
```
Only notifies if all 3 attempts fail.

---

### Fix 5 — Windows Unicode Encoding Errors (local only)

**Symptom:** `UnicodeEncodeError: 'cp1252' codec can't encode character` when running locally on Windows.

**Root cause 1:** Windows PowerShell uses cp1252 encoding by default — emoji in `print()` statements crash.

**Fix 1:** Force UTF-8 at script startup:
```python
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")
```

**Root cause 2:** HTTP headers (ntfy `Title:`) are latin-1 only — emoji in header values crash `requests`.

**Fix 2:** Keep emoji out of HTTP header values. Use ntfy text tags instead (they render as emoji in the app):
```python
# ❌ Emoji in header
headers={"Title": "🚨 Price Drop!"}

# ✅ Emoji in body, text tags in header
data="Price dropped to $2499".encode("utf-8")
headers={"Title": "B&H MacBook Price Drop!", "Tags": "rotating_light,apple,moneybag"}
```

The ntfy app renders `rotating_light` → 🚨, `apple` → 🍎, `moneybag` → 💰 automatically.

---

## 7. Product Management

Products are configured in `products.json`. No code changes needed to add, pause, or remove products.

### Schema

```json
{
  "name": "Display name (used in notifications)",
  "url": "Full B&H Photo product URL",
  "threshold": 2700.00,
  "active": true,

  // Optional fields (documentation only, not read by the script):
  "paused_reason": "Out of stock as of YYYY-MM-DD",
  "note": "Re-enable when back in stock",
  "purchased_at": "YYYY-MM-DD",
  "purchased_price": 2649.00
}
```

### Active Products (as of 2026-05-31)

| Model | URL slug | Threshold | Status |
|---|---|---|---|
| Silver 14" MBP M5 | `1957138-REG` | $2,700 | ⏸️ Paused — out of stock, was $2699 |
| Space Black 14" MBP M5 | `1957137-REG` | $2,700 | ⏸️ Paused temporarily |

### Common Operations

**Pause a product (e.g. out of stock):**
```json
"active": false,
"paused_reason": "Out of stock as of 2026-06-01"
```

**Resume tracking:**
```json
"active": true
```

**Record a purchase:**
```json
"active": false,
"purchased_at": "2026-06-15",
"purchased_price": 2649.00
```

**Add a new product on a different site:**
```json
{
  "name": "Sony A7C II (Amazon)",
  "url": "https://www.amazon.com/dp/B0CG5KG7GS",
  "threshold": 2000.00,
  "active": true
}
```

**After any change:** `git add products.json && git commit -m "..." && git push`

---

## 8. GitHub Actions Setup

### Workflow file: `.github/workflows/price_check.yml`

**Cron schedule:** `0 9 * * *` → 09:00 UTC daily (5:00 AM EDT / 2:00 AM PDT)

**Manual trigger:** Actions tab → B&H MacBook Price Check → Run workflow
- `price_override` input: enter a price (e.g. `2499.00`) to simulate a price drop without waiting for a real one

### Required Secrets

Set at: **Repo → Settings → Secrets and variables → Actions**

| Secret | Value |
|---|---|
| `SGAI_API_KEY` | From [scrapegraphai.com/dashboard](https://scrapegraphai.com/dashboard) |
| `NTFY_TOPIC` | `your-ntfy-topic` |

### Private Repo Note

This repo is **private** — no 60-day inactivity auto-disable applies (that only affects public repos). No keep-alive workflow needed.

### Pushing Code Changes

```powershell
git add <files>
git commit -m "description"
git push
```

For authentication, use a PAT with `repo` + `workflow` scopes, or set up credential caching:
```powershell
git config --global credential.helper manager
# Next push opens browser login once, then caches permanently
```

---

## 9. ntfy.sh Setup

**Topic:** `your-ntfy-topic` (pre-generated random string)

**Subscribe:** Install ntfy app → tap **+** → enter `your-ntfy-topic`

**Notification types:**

| Notification title | When | Priority |
|---|---|---|
| `B&H MacBook Price Drop!` | `current_price < threshold` | High (urgent) |
| `Price Tracker - Extraction Error` | SGAI failure, 504 (after all retries), sanity guard triggered | Default |

**Manual test (no API key needed):**
```bash
curl -d "Test message" -H "Title: Test" https://ntfy.sh/your-ntfy-topic
```

### Switching to Telegram

1. Create bot via [@BotFather](https://t.me/BotFather) → `/newbot` → copy token
2. Visit `https://api.telegram.org/bot<TOKEN>/getUpdates` after messaging your bot → copy `chat_id`
3. Add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` to `.env` + GitHub Secrets
4. In `check_prices.py`: uncomment `send_telegram()` and replace `notify_price_drop()` / `notify_extraction_error()` calls

---

## 10. Local Testing

### Setup

```powershell
py -3.12 -m pip install -r requirements.txt
# Fill in .env with SGAI_API_KEY and NTFY_TOPIC
```

### Test commands

```powershell
# 1. Test ntfy subscription (no API key needed)
curl -d "Test" -H "Title: Test" https://ntfy.sh/your-ntfy-topic

# 2. Simulate price drop → should trigger ntfy alert on phone
$env:PRICE_OVERRIDE="2499.00"; py -3.12 check_prices.py

# 3. Simulate price above threshold → should log "Above threshold", no alert
$env:PRICE_OVERRIDE="2799.00"; py -3.12 check_prices.py

# 4. Real live extraction (costs 10 credits per active product)
py -3.12 check_prices.py
```

### Simulate via GitHub Actions UI

Actions tab → B&H MacBook Price Check → Run workflow → enter `2499.00` in `price_override` field

---

## 11. Cost Management

### Current usage (1 active product)

| | Value |
|---|---|
| Calls/day | 1 |
| Calls/month | ~31 |
| Credits/month | ~310 |
| Free allocation | 500/month |
| **Headroom** | **190 credits/month** ✅ |

### If tracking 2 products simultaneously

| | Value |
|---|---|
| Credits/month | ~620 |
| Free allocation | 500/month |
| **Shortfall** | **120 credits/month** ❌ |

**Options when tracking 2+ products:**

| Option | Cron | Credits/mo (2 products) |
|---|---|---|
| Weekdays only | `0 9 * * 1-5` | ~440 ✅ |
| Every 2 days | `0 9 */2 * *` | ~310 ✅ |
| Daily + top-up | `0 9 * * *` | 620 (~$1.25/mo) |

**To change schedule:** edit `cron:` line in `price_check.yml`, commit, push.

---

## 12. Scaling to Other Products & Sites

### Site compatibility

| Site | Difficulty | Notes |
|---|---|---|
| B&H Photo | Medium | Cloudflare — requires `stealth=True` |
| Best Buy | Medium | Similar to B&H |
| Apple Store | Easy | Less aggressive bot detection |
| Amazon | Hard | Very aggressive — may need proxy rotation in future |

### Scaling beyond ~10 products

Current implementation is sequential. For 10+ products, switch to the async client:

```python
from scrapegraph_py import AsyncScrapeGraphAI
# Drop-in async replacement — no structural changes needed
```

---

## 13. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `ValidationError: Input should be a valid dictionary` | Passing Pydantic class as `schema=` | Use `MacBookProductSchema.model_json_schema()` |
| `'ExtractResponse' object has no attribute 'current_price'` | Accessing fields directly on `result.data` | Use `result.data.json_data["current_price"]` |
| `argument after ** must be a mapping, not ExtractResponse` | `MacBookProductSchema(**result.data)` | Use `result.data.json_data` (dict) |
| `http 504` error notification | SGAI gateway timeout (transient) | Retry logic handles this — only alerts after 3 failures. 0 credits charged. |
| `UnicodeEncodeError: cp1252` | Running on Windows with emoji in print/headers | Script auto-reconfigures to UTF-8. HTTP headers must be emoji-free. |
| `result.status == "error"`, credits depleted | SGAI credits exhausted | Check dashboard balance. Top up or reduce frequency. |
| `json_data is empty` | Cloudflare blocked request, AI read challenge page | Sanity guard fires ntfy warning. Try increasing `wait=` in FetchConfig. |
| `Price $0.00 outside valid range` | Extraction failure / page not loaded | Sanity guard fires. Not a false price drop. |
| No notification, price is above threshold | Working as intended | Check Actions run log for "Above threshold by $X" |
| Actions not running on schedule | GitHub cron can drift ±1hr | Expected. Not an error. |
| `Node.js 20 deprecation warning` | Old action versions | Use `actions/checkout@v5`, `actions/setup-python@v6` |
| Push rejected: `workflow` scope missing | PAT lacks workflow scope | Add `workflow` scope at github.com/settings/tokens |
