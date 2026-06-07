# Dealmoon BOGO Tracker — Complete Reference

This document is the authoritative reference for the Dealmoon BOGO deal tracker. It covers architecture, all design decisions, implementation details, post-launch fixes, configuration, testing, and troubleshooting. Read this before making any changes.

---

## Table of Contents

1. [What It Does](#1-what-it-does)
2. [Architecture](#2-architecture)
3. [File Structure](#3-file-structure)
4. [Configuration Reference](#4-configuration-reference)
5. [Core Logic — check_deals.py](#5-core-logic--check_dealspy)
6. [Retry & Orchestration Logic](#6-retry--orchestration-logic)
7. [State Management](#7-state-management)
8. [Notifications](#8-notifications)
9. [GitHub Actions Workflow](#9-github-actions-workflow)
10. [Testing — E2E with INCLUDE_EXPIRED](#10-testing--e2e-with-include_expired)
11. [Credit Usage](#11-credit-usage)
12. [Post-Launch Changes & Fixes](#12-post-launch-changes--fixes)
13. [Troubleshooting](#13-troubleshooting)
14. [How to Extend](#14-how-to-extend)

---

## 1. What It Does

Monitors Dealmoon's **official brand pages** (Clinique and Lancome) daily for "Buy One Get One" (BOGO) sale events. When a new active BOGO deal is detected, a high-priority push notification is sent to your phone via `ntfy.sh`.

**Why official brand pages only:** Dealmoon's brand pages with `type=1&sid=XXX` filter deals to the brand's own official website only. The clean landing URLs (e.g. `/en/top/lancome`) aggregate all stores including Macy's, Bloomingdale's, Nordstrom, etc. — which we do not want.

---

## 2. Architecture

```
GitHub Actions (daily 10:00 UTC)
    └── check_deals.py
            ├── Load deals_config.json
            ├── Load deals_state.json
            ├── ScrapeGraphAI → Lancome official page (js + stealth mode)
            │       └── Retry up to 3× (15s, 30s backoff)
            ├── ScrapeGraphAI → Clinique official page (js + stealth mode)
            │       └── Retry up to 3× (15s, 30s backoff)
            ├── Priority retry: if Lancome failed but Clinique succeeded
            │       └── Retry Lancome up to 3 more times
            ├── Full-flow retry: if ALL brands failed
            │       └── Sleep 2h → retry entire flow once
            │               └── If still all failed → ntfy error alert
            ├── Keyword filter (BOGO / buy one get one / b1g1 / ...)
            ├── Deduplication check (deals_state.json)
            └── ntfy.sh high-priority push → 📱 your phone
```

**No server required.** Runs entirely on GitHub's free infrastructure.

---

## 3. File Structure

```
beautiful-salk/
├── .env                          ← Local secrets (gitignored)
├── .gitignore                    ← Excludes .env, deals_state.json
├── requirements.txt              ← scrapegraph-py, python-dotenv, requests
├── check_deals.py                ← Core BOGO tracker (this tracker)
├── deals_config.json             ← Brand configuration
├── deals_state.json              ← Runtime dedup state (gitignored, auto-created)
├── check_prices.py               ← MacBook B&H price tracker (currently paused)
├── products.json                 ← MacBook products config (all active: false)
├── DEALMOON_TRACKER_REFERENCE.md ← This file
├── README.md                     ← Setup and run guide
└── .github/
    └── workflows/
        ├── deal_check.yml        ← Dealmoon BOGO daily cron
        └── price_check.yml       ← MacBook price check (runs but skips all — paused)
```

---

## 4. Configuration Reference

### `.env` — Local Secrets (never commit)

```bash
SGAI_API_KEY=sgai-...        # ScrapeGraphAI API key
NTFY_TOPIC=your-ntfy-topic   # ntfy.sh topic name
GITHUB_PAT=ghp_...           # GitHub Personal Access Token (Contents: read+write)
                             # Used for non-interactive git push from background scripts
# INCLUDE_EXPIRED=           # Leave blank for normal runs; set to 'true' for E2E testing
```

### `deals_config.json` — Brand Configuration

```json
[
  {
    "brand": "Lancome",
    "url": "https://www.dealmoon.com/en/top/lancome?type=1&click=0&sort=relevance&expiredClick=n&exp=y&sid=968",
    "keywords": ["buy one get one", "bogo", "b1g1", "buy 1 get 1", "buy one, get one"],
    "active": true
  },
  {
    "brand": "Clinique",
    "url": "https://www.dealmoon.com/en/top/clinique?type=1&click=0&sort=relevance&expiredClick=n&exp=y&sid=184",
    "keywords": ["buy one get one", "bogo", "b1g1", "buy 1 get 1", "buy one, get one"],
    "active": true
  }
]
```

**Key URL parameters:**
| Parameter | Value | Meaning |
|---|---|---|
| `type=1` | Fixed | Filter to official brand site only |
| `expiredClick=n` | Normal run | Dealmoon only serves active deals |
| `expiredClick=y` | E2E test mode | Dealmoon serves both active + expired deals |
| `sid=968` | Lancome ID | Brand store identifier |
| `sid=184` | Clinique ID | Brand store identifier |

> [!IMPORTANT]
> **Do NOT use the clean landing URLs** (`/en/top/lancome`, `/en/top/clinique`) as fallback. These aggregate all retailers (Macy's, Bloomingdale's, Nordstrom, etc.) and would trigger false alerts for non-official-site BOGO deals.

**Order matters:** Lancome is listed first because it has higher priority (see retry logic below).

**To pause a brand:** Set `"active": false`. No code changes needed.

**To add a new brand:** Add a new JSON object with `brand`, `url`, `keywords`, and `"active": true`. Find the `sid` parameter by visiting the brand's Dealmoon page and copying the URL.

---

## 5. Core Logic — `check_deals.py`

### Pydantic Extraction Schema

```python
class Deal(BaseModel):
    title:       str   # Deal headline
    description: str   # Full details, promo codes, gift items, expiry
    deal_date:   str   # Date posted/started (e.g. "May 30", "2026-05-30"), empty if not found
    is_expired:  bool  # True if marked expired on Dealmoon page
```

```python
class DealmoonSchema(BaseModel):
    deals: list[Deal]  # All deals on the page
```

### SGAI Extraction Settings

```python
fetch_config = FetchConfig(mode="js", stealth=True, wait=4000)
```

- `mode="js"`: JavaScript rendering required (Dealmoon is a React SPA)
- `stealth=True`: Bypass Akamai bot detection (+5 credits per call)
- `wait=4000`: 4-second wait for content to fully render

**Cost:** 10 credits per extraction call (5 base + 5 stealth).

### Keyword Matching

Case-insensitive match against the concatenation of `title + " " + description`:

```python
keywords = ["buy one get one", "bogo", "b1g1", "buy 1 get 1", "buy one, get one"]
combined = f"{title} {description}".lower()
matched  = any(kw in combined for kw in keywords)
```

### INCLUDE_EXPIRED Mode

When `INCLUDE_EXPIRED=true`, the URL parameter is modified programmatically:

```python
url = url.replace("expiredClick=n", "expiredClick=y")
```

This ensures Dealmoon serves historical expired deals, enabling real-data E2E pipeline testing without fake/injected data. Expired deals are **never** processed in normal daily runs — the `is_expired` field filters them out.

---

## 6. Retry & Orchestration Logic

This is the most critical part of the design. Read carefully before modifying.

```
STEP 1: Extract Lancome (attempts 1-3, delays: 15s, 30s)
STEP 2: Extract Clinique (attempts 1-3, delays: 15s, 30s)

EVALUATE:
├── Both succeed            → proceed to filter + notify
├── Lancome fail, Clinique ok → STEP 3: retry Lancome (3 more attempts)
│       ├── Lancome now ok  → proceed to filter + notify (both results)
│       └── Lancome still fail → proceed with Clinique results only
├── Clinique fail, Lancome ok → proceed with Lancome results only
└── Both fail               → STEP 4: sleep FLOW_RETRY_WAIT (2h)
        └── Re-run STEP 1+2
                ├── Any succeed → proceed to filter + notify
                └── All still fail → send ntfy error alert, exit 1
```

**Why asymmetric retry for Lancome?** Lancome has higher priority. If it fails on the first pass but we already have Clinique results, we specifically retry Lancome before giving up, to maximize the chance of catching Lancome BOGO events.

**Why 2-hour full-flow retry?** Dealmoon or SGAI sometimes experience brief outages of 30-60 minutes. Waiting 2 hours and retrying the entire flow once catches these transient issues without sending a noisy error notification to your phone.

**GitHub Actions runner minutes implication:** If the 2-hour retry is triggered, the job runs for 2+ hours, consuming ~120 runner minutes. With 2,000 free minutes/month, this is acceptable for rare failure events.

---

## 7. State Management

**File:** `deals_state.json` (gitignored, auto-created at runtime)

```json
{
  "notified_titles": [
    "[Expired]17th Exclusive: Clinique Select Bestsellers Buy 1 Get 1 Free",
    "Ending Soon: Lancome Selected Beauty Sale 2-Day BOGO"
  ]
}
```

**How it works:**
- Before sending any alert, the script checks if the deal title is already in `notified_titles`.
- After sending an alert, the title is appended and the file is saved.
- This prevents duplicate notifications across daily runs for the same ongoing sale event.

**When to reset:** If you want to be re-notified about a currently active deal you already received (e.g. after purchasing), delete the title from `notified_titles` manually or clear the array entirely.

**Do not commit `deals_state.json`:** It is in `.gitignore`. If committed, it would suppress re-notifications when a fresh environment (like GitHub Actions) checks out the repo.

---

## 8. Notifications

### ntfy.sh Push Notification

**BOGO Alert (high priority):**
- Title: `🛍️ {Brand} BOGO Sale on Dealmoon!` (ASCII-sanitized for HTTP header)
- Body:
  ```
  {deal_title}
  Posted: {deal_date}

  {description}
  ```
- Priority: `high`
- Tags: `rotating_light,lipstick,moneybag`

**Extraction Error Alert (default priority):**
- Title: `Deal Tracker — Extraction Failed`
- Body: Lists which brands failed + note that daily retry will resume tomorrow
- Priority: `default`
- Tags: `warning`

### Unicode Header Fix

HTTP headers must be latin-1 encodeable. Dealmoon deal titles frequently contain non-latin-1 characters (e.g. `：` fullwidth colon, `—` em dash, `》`). The `Title` header is sanitised before sending:

```python
safe_title = title.encode("ascii", errors="replace").decode("ascii")
```

The full Unicode title is preserved in the UTF-8 body, so your phone displays the original text correctly.

---

## 9. GitHub Actions Workflow

**File:** `.github/workflows/deal_check.yml`

```yaml
name: Dealmoon BOGO Daily Check

on:
  schedule:
    - cron: "0 10 * * *"   # Daily at 10:00 UTC (6:00 AM EDT)
  workflow_dispatch:
    inputs:
      include_expired:
        description: "Set to 'true' for real-data E2E pipeline testing"
        default: "false"

jobs:
  check-deals:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - run: pip install -r requirements.txt
      - name: Run BOGO check
        env:
          SGAI_API_KEY:    ${{ secrets.SGAI_API_KEY }}
          NTFY_TOPIC:      ${{ secrets.NTFY_TOPIC }}
          INCLUDE_EXPIRED: ${{ inputs.include_expired || 'false' }}
        run: python check_deals.py
```

**Required GitHub Secrets:**
| Secret | Value |
|---|---|
| `SGAI_API_KEY` | Your ScrapeGraphAI API key |
| `NTFY_TOPIC` | Your ntfy.sh topic name |

**Note:** `GITHUB_PAT` is stored in `.env` locally only — it is not needed in GitHub Actions secrets because GitHub Actions checks out the repo directly via `actions/checkout`.

---

## 10. Testing — E2E with INCLUDE_EXPIRED

> [!CAUTION]
> **Preserve SGAI credits.** Do NOT run `python check_deals.py` locally for testing. Each run consumes 20 credits (2 brands × 10 credits). Use `INCLUDE_EXPIRED=true` only via GitHub Actions manual trigger.

### How to Run E2E Test

1. Go to **[github.com/xuzhang78/PriceTracker](https://github.com/xuzhang78/PriceTracker) → Actions → Dealmoon BOGO Daily Check → Run workflow**
2. Set `include_expired` to **`true`**
3. Click **Run workflow**

**What to verify in the run log:**
- Both brands extracted successfully (may require retries — this is expected)
- BOGO keyword matches found in historical expired deals
- `✅ BOGO alert sent via ntfy!` appears for Clinique and Lancome
- `💾 State saved to deals_state.json.` appears

**What to verify on your phone:**
- High-priority `ntfy` push notifications received for both brands
- Notification body contains the deal title, date, and description

**Deduplication check:** Run the workflow a second time with `include_expired=true`. Verify the log shows `ℹ️ Already notified — skipping duplicate alert.` for all previously matched deals.

**After testing:** The `deals_state.json` in the Actions environment is ephemeral (not committed). Your local `deals_state.json` is unaffected. This is by design — each daily run starts with a clean state in GitHub Actions, which means you will re-receive alerts if an active BOGO deal is still ongoing the next day. Only the local state prevents repeated local notifications.

> [!NOTE]
> **Re-evaluating state management for production:** Since `deals_state.json` is gitignored and not committed, GitHub Actions has no persistent state across runs. This means if a BOGO sale runs for multiple days, you will receive a notification every day. If this becomes noisy, consider committing `deals_state.json` to the repo and adding a workflow step to commit it back after each run.

### ✅ Confirmed E2E Test Result — May 31, 2026

**Trigger:** GitHub Actions manual run with `include_expired=true`
**Python version:** 3.12 (after fixing from 3.11)

**Results:**
- 6 ntfy push notifications received on phone ✅
  - 3 × Lancome expired BOGO deals (Lancome runs first per config order)
  - 3 × Clinique expired BOGO deals
- All deals had correct keyword matches (`bogo`, `buy 1 get 1`)
- Both brands extracted successfully from the official sid-filtered URLs
- State file updated in the ephemeral Actions environment

**Conclusion:** Full pipeline confirmed working end-to-end on GitHub Actions. Daily cron (`0 10 * * *`) is now live and will alert on the next active BOGO sale event.

---

## 11. Credit Usage

| Operation | Credits/call | Calls/day | Credits/day | Credits/month |
|---|---|---|---|---|
| SGAI extract (js + stealth) | 10 | 2 brands | 20 | ~600 |
| Full-flow retry (rare) | 10 | +2 | +20 | Negligible |

**Free plan:** 500 credits/month + 500 one-time bonus.
- Monthly overage: ~100 credits/month (covered by the 500 one-time buffer for several months).
- **$5 one-time top-up** (1,000 non-expiring credits) provides a comfortable long-term buffer.

**MacBook tracker:** Currently paused (`"active": false`). Uses 0 credits/month.

---

## 12. Post-Launch Changes & Fixes

### v2 → v2.1: Python Version Fix in GitHub Actions Workflow

**Date:** May 31, 2026 (first GitHub Actions manual trigger)

**Issue:** `pip install -r requirements.txt` failed in GitHub Actions with:
```
ERROR: Could not find a version that satisfies the requirement scrapegraph-py>=2.1.0
ERROR: Ignored the following versions that require a different python version:
       2.0.1 Requires-Python >=3.12; 2.1.0 Requires-Python >=3.12
```

**Root cause:** `deal_check.yml` was configured with `python-version: "3.11"`. The `scrapegraph-py` v2.x package requires **Python 3.12+**.

**Fix:** Updated `deal_check.yml` to Python 3.12 and latest action versions to match `price_check.yml`:
```diff
-      - uses: actions/checkout@v4
+      - uses: actions/checkout@v5
-      - uses: actions/setup-python@v5
+      - uses: actions/setup-python@v6
         with:
-          python-version: "3.11"
+          python-version: "3.12"
```

---

### v2.1 → v2.2: False-Positive Expired Deal Alert Fix

**Date:** June 4, 2026 (reported from daily production run)

**Symptom:** User received a push notification for an expired Clinique BOGO deal during a normal daily run (no `INCLUDE_EXPIRED`).

**Root cause:** `process_results()` determined expired status solely from the AI-returned `is_expired` field. ScrapeGraphAI occasionally misclassifies an expired deal as `is_expired=False` (AI hallucination), causing it to pass the filter and trigger a false alert.

**Fix:** Added a second, server-side signal — the `[Expired]` title prefix. Dealmoon always prepends `[Expired]` to expired deal titles at the server level, making it a reliable indicator independent of AI accuracy:
```python
# Before (single source — AI can hallucinate)
is_expired = deal.get("is_expired", False)

# After (dual check — either signal is sufficient)
ai_expired    = deal.get("is_expired", False)
title_expired = title.lower().startswith("[expired]")
is_expired    = ai_expired or title_expired
```
Also removed a leftover `[DEBUG]` print statement from `process_results()`.

---

### v2.2 → v2.3: Expired Deals Served Server-Side via `exp=y` URL Parameter

**Date:** June 6, 2026 (reported from daily production run — same symptom as v2.2)

**Symptom:** User again received a push notification for an expired Clinique BOGO deal despite the v2.2 `[Expired]` title-prefix fix.

**Root cause (deeper):** The v2.2 fix addressed AI misclassification, but the **deeper root cause was never fixed**: the config URLs contain `exp=y`, which is the **server-side** Dealmoon parameter that instructs the server to include expired deals in the page response. The previous `_build_url()` fix only swapped `expiredClick=n → y` (a UI toggle), which had **no effect** on what the server actually returned. So every daily run was receiving a page with both active and expired deals mixed together, relying entirely on the AI to classify them correctly — which it occasionally could not.

Additionally, **Lancome was found with `"active": false`** in `deals_config.json`, meaning it was silently not being tracked.

**Fix 1 — URL parameter (root cause):** `_build_url()` now correctly handles `exp`:
```python
# Before: only swapped expiredClick (UI toggle — no effect on server response)
if include_expired:
    return base_url.replace("expiredClick=n", "expiredClick=y")
return base_url   # ← exp=y remained! Server still sent expired deals.

# After: in normal daily runs, force exp=n at the server level
if include_expired:
    return base_url.replace("expiredClick=n", "expiredClick=y")  # exp=y kept
else:
    return base_url.replace("exp=y", "exp=n")  # server excludes expired deals
```

**Fix 2 — Lancome restored:** `deals_config.json` Lancome entry restored to `"active": true`.

**Defense in depth:** The `[Expired]` title-prefix check from v2.2 is retained as a safety net in case the server still occasionally leaks an expired deal through.

---

### v1 → v1.1: `UnicodeEncodeError` Bug Fix




**Date:** May 31, 2026 (during initial local testing)

**Issue:** The `notify_extraction_error()` function crashed with:
```
UnicodeEncodeError: 'latin-1' codec can't encode character '\u2014' in position 13
```

**Root cause:** HTTP headers must be latin-1 encodeable. The `Title` header contained an em dash `—` from a Dealmoon deal title.

**Fix:** Sanitise the Title header to ASCII before every `ntfy` POST:
```python
safe_title = title.encode("ascii", errors="replace").decode("ascii")
```

---

### v1.1 → v2: Full Refactor (6 Changes)

**Date:** May 31, 2026 (post-launch improvements)

#### Change 1 — Expired deals: testing only
**Before:** `INCLUDE_EXPIRED=true` only changed the SGAI prompt instruction.
**After:** Also swaps `expiredClick=n → y` in the URL itself, so Dealmoon actually serves expired deals to the page scraper. Without this, Dealmoon's server-side filtering hides expired deals before they reach the AI extractor.

#### Change 2 — `deal_date` field added
**Before:** Schema had `title`, `description`, `is_expired`.
**After:** Added `deal_date: str` — instructs SGAI to extract the date the deal was posted or started. Useful to identify which BOGO is the most recent when multiple matches appear. Included in the ntfy notification as `Posted: {date}`.

#### Change 3 — Removed fallback to clean URLs
**Before:** On primary URL failure, the script fell back to `/en/top/clinique` and `/en/top/lancome`.
**After:** Fallback removed entirely. The clean landing URLs aggregate deals from all retailers (Macy's, Nordstrom, etc.), violating the requirement to only track official Clinique/Lancome website promotions. Retry logic alone handles transient failures.

#### Change 4 — Lancome runs first (higher priority)
**Before:** Clinique was first in `deals_config.json` (alphabetical from initial setup).
**After:** Lancome is first. The script processes brands in config order, so Lancome is always extracted first.

#### Change 5 — New orchestrated retry logic
**Before:** Simple 3-attempt retry per brand, independent.
**After:** Fully orchestrated flow:
1. Lancome (3 attempts) then Clinique (3 attempts).
2. If Lancome fails but Clinique succeeds → retry Lancome specifically (3 more attempts).
3. If all brands fail → sleep 2 hours → retry entire flow once.
4. If retry flow also fails → send ntfy error notification.

#### Change 6 — Credit conservation
**Before:** Local test runs used SGAI credits freely.
**After:** Policy: no local SGAI calls for testing. All end-to-end testing done exclusively via GitHub Actions manual trigger with `include_expired=true`.

---

## 13. Troubleshooting

| Symptom | Likely Cause | Fix |
|---|---|---|
| `HTTP 502` / `HTTP 504` on extraction | Transient SGAI/Akamai proxy issue | Expected — retry logic handles this automatically |
| All 3 retries fail for a brand | Prolonged SGAI outage or Dealmoon blocking | Full-flow retry (2h sleep) kicks in automatically |
| No BOGO match found | No active BOGO sale today | Correct — no alert is expected |
| Duplicate alerts received | `deals_state.json` reset or deleted | Check state file; verify it persists between runs |
| ntfy alert has `?` characters in title | Non-ASCII characters replaced in header | Expected — full title is preserved in the body |
| `deals_state.json` grows large | Many historical deals tracked | Safe to manually trim old entries from `notified_titles` array |
| GitHub Actions run fails with `missing env var` | Secret not set in GitHub repo | Add `SGAI_API_KEY` and `NTFY_TOPIC` to repo Secrets |
| `git push` fails in background script | No interactive credential dialog available | Use `GITHUB_PAT` in `.env` via the push helper script |

---

## 14. How to Extend

### Add a New Brand

1. Find the brand's Dealmoon official page URL (must have `type=1&sid=XXX`).
2. Add an entry to `deals_config.json`:
```json
{
  "brand": "Estee Lauder",
  "url": "https://www.dealmoon.com/en/top/estee-lauder?type=1&click=0&sort=relevance&expiredClick=n&exp=y&sid=XXX",
  "keywords": ["buy one get one", "bogo", "b1g1", "buy 1 get 1"],
  "active": true
}
```
3. Place it in priority order (highest priority brand first).
4. No code changes required.

**Credit impact:** Each additional brand adds 10 credits/day = 300 credits/month.

### Change BOGO Keywords

Edit the `keywords` array for the relevant brand in `deals_config.json`. Keywords are matched case-insensitively against `title + description`.

### Re-enable MacBook Price Tracking

Set `"active": true` in `products.json` for the desired model. The `price_check.yml` workflow runs daily at 09:00 UTC and will resume checking immediately.

### Switch Notification Channel to Telegram

1. Create a bot via `@BotFather` on Telegram → copy Bot Token.
2. Send a message to the bot, then visit `https://api.telegram.org/bot<TOKEN>/getUpdates` → copy Chat ID.
3. Add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` to `.env` and GitHub Secrets.
4. Replace `_send_ntfy()` calls in `check_deals.py` with a `send_telegram()` function.
