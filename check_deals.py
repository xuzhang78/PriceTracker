"""
Dealmoon BOGO Sale Event Tracker
---------------------------------
Reads deals_config.json, uses ScrapeGraphAI to AI-extract deals from Dealmoon
brand pages, and sends an ntfy.sh push notification when a "Buy One Get One"
(BOGO) event is detected on an active deal.

Resilience features:
  - 3-attempt retry loop with exponential backoff (15s, 30s) for transient
    HTTP 502/504 errors from ScrapeGraphAI's browser proxy.
  - Automatic fallback to a clean canonical landing URL if all retries on the
    primary complex URL fail.
  - Defensive dict-based parsing of ExtractResponse.json_data to avoid
    attribute-access errors from SDK version differences.

State management:
  - Tracks notified deal titles in deals_state.json to prevent duplicate alerts.

Testing mode:
  - Set INCLUDE_EXPIRED=true to include expired deals from Dealmoon.
    This is used for real-data end-to-end pipeline testing (e.g. via GitHub
    Actions manual trigger) without injecting any fake data.

Usage:
  Normal run:          python check_deals.py
  E2E pipeline test:   INCLUDE_EXPIRED=true python check_deals.py  (Linux/macOS)
                       $env:INCLUDE_EXPIRED="true"; python check_deals.py  (PowerShell)
"""

import json
import os
import sys
import time

# Force UTF-8 output so emoji print correctly on Windows terminals
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

import requests
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from scrapegraph_py import FetchConfig, ScrapeGraphAI

# ── Configuration ──────────────────────────────────────────────────────────────

load_dotenv()

SGAI_API_KEY     = os.getenv("SGAI_API_KEY", "")
NTFY_TOPIC       = os.getenv("NTFY_TOPIC", "")
INCLUDE_EXPIRED  = os.getenv("INCLUDE_EXPIRED", "false").strip().lower() == "true"

MAX_RETRIES      = 3
RETRY_DELAYS     = [15, 30]   # seconds between attempts (exponential backoff)

BASE_DIR         = os.path.dirname(__file__)
CONFIG_PATH      = os.path.join(BASE_DIR, "deals_config.json")
STATE_PATH       = os.path.join(BASE_DIR, "deals_state.json")


# ── Pydantic Schema ────────────────────────────────────────────────────────────

class Deal(BaseModel):
    """A single deal/promotion extracted from a Dealmoon brand page."""
    title:      str  = Field(description="The main title or headline of the deal")
    description: str = Field(description="Full details of the deal including terms, promo codes, or gifts")
    is_expired: bool = Field(description="True if the deal is explicitly marked as expired on the page, otherwise False")


class DealmoonSchema(BaseModel):
    """Wrapper schema for a list of deals extracted from a Dealmoon page."""
    deals: list[Deal] = Field(description="All deals listed on the page")


# ── State Management ───────────────────────────────────────────────────────────

def load_state() -> dict:
    """Load deals_state.json, returning a default structure if missing or corrupt."""
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"notified_titles": []}


def save_state(state: dict) -> None:
    """Persist the updated state dict to deals_state.json."""
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def already_notified(state: dict, title: str) -> bool:
    return title in state.get("notified_titles", [])


def mark_notified(state: dict, title: str) -> None:
    state.setdefault("notified_titles", []).append(title)


# ── Notification Helpers ───────────────────────────────────────────────────────

def _send_ntfy(title: str, body: str, priority: str = "default", tags: str = "") -> None:
    """POST a push notification to ntfy.sh.

    HTTP headers must be latin-1 encodeable. Non-latin-1 characters (e.g. em
    dashes, Chinese characters) in the Title header cause a UnicodeEncodeError.
    We sanitise the header value to ASCII, replacing un-encodeable characters
    with '?', while keeping the full Unicode text in the (UTF-8) body.
    """
    safe_title = title.encode("ascii", errors="replace").decode("ascii")
    try:
        response = requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=body.encode("utf-8"),
            headers={
                "Title":    safe_title,
                "Priority": priority,
                "Tags":     tags,
            },
            timeout=10,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        print(f"    ⚠️  ntfy send failed: {exc}")


def notify_bogo_deal(brand: str, deal_title: str, description: str) -> None:
    """Send a high-priority BOGO sale alert."""
    _send_ntfy(
        title=f"🛍️ {brand} BOGO Sale on Dealmoon!",
        body=f"{deal_title}\n\n{description}",
        priority="high",
        tags="rotating_light,lipstick,moneybag",
    )
    print("    ✅ BOGO alert sent via ntfy!")


def notify_extraction_error(brand: str, reason: str) -> None:
    """Send a warning alert when extraction fails after all retries."""
    _send_ntfy(
        title="Deal Tracker — Extraction Error",
        body=f"Failed to extract deals for: {brand}\n\nReason: {reason}",
        priority="default",
        tags="warning",
    )
    print("    ⚠️  Error alert sent via ntfy!")


# ── Core Extraction Logic ──────────────────────────────────────────────────────

def _extract_once(sgai: ScrapeGraphAI, url: str, include_expired: bool) -> dict | None:
    """
    Attempt a single ScrapeGraphAI extraction on the given URL.
    Returns the json_data dict on success, or None on failure.
    """
    expired_instruction = (
        "Include both active and expired deals."
        if include_expired
        else "Only include active (non-expired) deals."
    )
    try:
        result = sgai.extract(
            url=url,
            schema=DealmoonSchema.model_json_schema(),
            prompt=(
                f"Extract all deals listed on the page. For each deal, get the title, "
                f"the full description, and whether it is marked as expired. {expired_instruction}"
            ),
            fetch_config=FetchConfig(mode="js", stealth=True, wait=4000),
        )
        if result.status == "success":
            data = result.data.json_data
            if data and isinstance(data, dict):
                return data
            print(f"      ⚠️  Extraction succeeded but json_data is empty or malformed.")
            return None
        else:
            print(f"      ⚠️  SDK returned error status: {result.error}")
            return None
    except Exception as exc:
        print(f"      ⚠️  Exception during extraction: {exc}")
        return None


def extract_deals(sgai: ScrapeGraphAI, brand: str, primary_url: str, fallback_url: str, include_expired: bool) -> list[dict] | None:
    """
    Attempt extraction with retry logic on the primary URL.
    If all retries fail, fall back to the clean canonical URL.
    Returns a list of raw deal dicts, or None if everything fails.
    """
    last_error: str = ""

    # ── Primary URL with retries ──────────────────────────────────────────────
    for attempt in range(1, MAX_RETRIES + 1):
        print(f"  Attempt {attempt}/{MAX_RETRIES} (primary URL)...")
        data = _extract_once(sgai, primary_url, include_expired)
        if data is not None:
            deals = data.get("deals", [])
            print(f"  ✅ Extracted {len(deals)} deal(s) from primary URL.")
            return deals

        last_error = f"Attempt {attempt} failed on primary URL."
        if attempt < MAX_RETRIES:
            delay = RETRY_DELAYS[attempt - 1]
            print(f"  Waiting {delay}s before retry...")
            time.sleep(delay)

    # ── Fallback URL (single attempt) ─────────────────────────────────────────
    print(f"  🚨 All retries exhausted on primary URL. Falling back to clean URL...")
    data = _extract_once(sgai, fallback_url, include_expired)
    if data is not None:
        deals = data.get("deals", [])
        print(f"  ✅ Extracted {len(deals)} deal(s) from fallback URL.")
        return deals

    # Both primary and fallback failed
    print(f"  ❌ Extraction failed on both primary and fallback URLs.")
    notify_extraction_error(brand, f"All retries exhausted + fallback failed. Last: {last_error}")
    return None


# ── Deal Checking ──────────────────────────────────────────────────────────────

def check_brand(sgai: ScrapeGraphAI, brand_cfg: dict, state: dict) -> bool:
    """
    Process one brand entry from deals_config.json.
    Returns True if state was modified (a new alert was sent).
    """
    brand        = brand_cfg["brand"]
    primary_url  = brand_cfg["url"]
    fallback_url = brand_cfg["fallback_url"]
    keywords     = [kw.lower() for kw in brand_cfg.get("keywords", [])]

    print(f"\n→ {brand}")
    print(f"  Primary URL:  {primary_url}")
    print(f"  Fallback URL: {fallback_url}")
    print(f"  Keywords:     {keywords}")
    print(f"  Mode:         {'include expired (E2E test)' if INCLUDE_EXPIRED else 'active deals only'}")

    deals = extract_deals(sgai, brand, primary_url, fallback_url, INCLUDE_EXPIRED)
    if deals is None:
        return False

    state_modified = False

    for deal in deals:
        # Defensive parsing — use .get() to avoid KeyError on malformed entries
        title       = deal.get("title", "").strip()
        description = deal.get("description", "").strip()
        is_expired  = deal.get("is_expired", False)

        if not title:
            continue

        # Skip expired deals in normal daily runs
        if is_expired and not INCLUDE_EXPIRED:
            continue

        # Keyword match against title and description (case-insensitive)
        combined  = f"{title} {description}".lower()
        matched_kw = next((kw for kw in keywords if kw in combined), None)
        if not matched_kw:
            continue

        print(f"\n  🎯 BOGO match! Keyword: '{matched_kw}'")
        print(f"     Title:  {title}")
        print(f"     Status: {'EXPIRED' if is_expired else 'ACTIVE'}")

        # Deduplication check
        if already_notified(state, title):
            print(f"     ℹ️  Already notified — skipping duplicate alert.")
            continue

        # Send notification and record in state
        notify_bogo_deal(brand, title, description)
        mark_notified(state, title)
        state_modified = True

    return state_modified


# ── Entry Point ────────────────────────────────────────────────────────────────

def main() -> None:
    # Validate required env vars
    missing = [k for k, v in {"SGAI_API_KEY": SGAI_API_KEY, "NTFY_TOPIC": NTFY_TOPIC}.items() if not v]
    if missing:
        print(f"❌ Missing required environment variable(s): {', '.join(missing)}")
        print("   Set them in your .env file (local) or GitHub Secrets (CI).")
        sys.exit(1)

    if INCLUDE_EXPIRED:
        print("⚠️  Running in INCLUDE_EXPIRED mode — expired deals will be processed for E2E testing.\n")

    # Load configuration
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        all_brands = json.load(f)

    active_brands = [b for b in all_brands if b.get("active", True)]
    if not active_brands:
        print("No active brands in deals_config.json. Nothing to check.")
        sys.exit(0)

    print(f"🔍 Checking {len(active_brands)} active brand(s)...")

    # Load persistent state
    state = load_state()

    sgai = ScrapeGraphAI(api_key=SGAI_API_KEY)
    state_modified = False

    for brand_cfg in active_brands:
        modified = check_brand(sgai, brand_cfg, state)
        if modified:
            state_modified = True
        print()

    # Persist state only if something changed
    if state_modified:
        save_state(state)
        print("💾 State saved to deals_state.json.")

    print("✅ All checks complete.")


if __name__ == "__main__":
    main()
