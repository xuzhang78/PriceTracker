"""
Dealmoon BOGO Sale Event Tracker
---------------------------------
Reads deals_config.json, uses ScrapeGraphAI to AI-extract deals from Dealmoon
official-brand pages (sid-filtered URLs only — no multi-store aggregator pages),
and sends an ntfy.sh push notification when a new "Buy One Get One" (BOGO)
active deal is detected.

Retry logic:
  1. Extract Lancome (up to 3 attempts), then Clinique (up to 3 attempts).
  2. If Lancome failed but Clinique succeeded → retry Lancome up to 3 more times.
  3. If ALL brands failed → wait FLOW_RETRY_WAIT hours, then retry entire flow once.
  4. If retry flow also fails → send error notification and exit 1.

State management:
  - deals_state.json tracks notified deal titles to prevent duplicate alerts.

Testing mode (E2E via GitHub Actions only — do NOT run locally to preserve credits):
  - INCLUDE_EXPIRED=true: swaps expiredClick=n → y in the URL so Dealmoon also
    serves expired deals, enabling real-data pipeline verification without fake data.
    Expired deals are NEVER processed in daily (normal) runs.

Usage:
  Normal daily run:   python check_deals.py
  E2E pipeline test:  INCLUDE_EXPIRED=true python check_deals.py
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

# Per-brand retry settings
MAX_RETRIES      = 3
RETRY_DELAYS     = [15, 30]        # seconds between attempts (exponential backoff)

# Full-flow retry: wait this long before retrying all brands when everything fails
FLOW_RETRY_WAIT  = 2 * 3600       # 2 hours

BASE_DIR         = os.path.dirname(__file__)
CONFIG_PATH      = os.path.join(BASE_DIR, "deals_config.json")
STATE_PATH       = os.path.join(BASE_DIR, "deals_state.json")


# ── Pydantic Schema ────────────────────────────────────────────────────────────

class Deal(BaseModel):
    """A single deal/promotion extracted from a Dealmoon official-brand page."""
    title:       str  = Field(description="The main title or headline of the deal")
    description: str  = Field(
        description="Full details of the deal including terms, promo codes, gift items, or expiry"
    )
    deal_date:   str  = Field(
        description=(
            "The date the deal started or was posted on Dealmoon. Look for date labels "
            "near the deal title or post timestamp (e.g. 'May 30', '2026-05-30'). "
            "Return as a human-readable string, or empty string if not found."
        )
    )
    is_expired:  bool = Field(
        description="True if the deal is explicitly marked as expired on the page, otherwise False"
    )


class DealmoonSchema(BaseModel):
    """Wrapper schema for all deals on a Dealmoon official-brand page."""
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

    HTTP headers must be latin-1 encodeable. Non-latin-1 characters in deal
    titles (e.g. fullwidth colons, em dashes from Dealmoon) cause a
    UnicodeEncodeError. We sanitise the Title header to ASCII (replacing
    un-encodeable chars with '?') while preserving full Unicode in the body.
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


def notify_bogo_deal(brand: str, deal_title: str, description: str, deal_date: str) -> None:
    """Send a high-priority BOGO deal alert."""
    date_line = f"Posted: {deal_date}\n" if deal_date else ""
    _send_ntfy(
        title=f"🛍️ {brand} BOGO Sale on Dealmoon!",
        body=f"{deal_title}\n{date_line}\n{description}",
        priority="high",
        tags="rotating_light,lipstick,moneybag",
    )
    print("    ✅ BOGO alert sent via ntfy!")


def notify_extraction_error(brands: list[str]) -> None:
    """Send a warning alert when all retries and the full-flow retry have failed."""
    _send_ntfy(
        title="Deal Tracker — Extraction Failed",
        body=(
            f"All extraction attempts failed for: {', '.join(brands)}\n\n"
            "Both the initial attempts and the 2-hour retry flow were exhausted. "
            "Will retry at the next scheduled daily run."
        ),
        priority="default",
        tags="warning",
    )
    print("  ⚠️  Error alert sent via ntfy!")


# ── Core Extraction ────────────────────────────────────────────────────────────

def _build_url(base_url: str, include_expired: bool) -> str:
    """
    Build the URL to scrape, controlling whether Dealmoon serves expired deals.

    The config URLs contain exp=y which is the SERVER-SIDE parameter that tells
    Dealmoon to include expired deals in the page response (independent of the
    expiredClick UI-toggle). In normal daily runs we force exp=n so the server
    only returns active deals — eliminating expired content before it even
    reaches the AI, rather than relying solely on AI classification.

    In INCLUDE_EXPIRED (E2E test) mode we keep exp=y and also flip expiredClick
    to ensure all historical expired deals are served for pipeline verification.
    """
    if include_expired:
        # E2E test: serve active + expired deals
        return base_url.replace("expiredClick=n", "expiredClick=y")
    else:
        # Normal daily run: force server-side exclusion of expired deals
        return base_url.replace("exp=y", "exp=n")


def _extract_once(sgai: ScrapeGraphAI, url: str, include_expired: bool) -> dict | None:
    """
    Single ScrapeGraphAI extraction attempt on the given URL.
    Returns the raw json_data dict on success, or None on any failure.
    Uses defensive dict access — never trusts attribute access on ExtractResponse.
    """
    expired_instruction = (
        "Include both active and expired deals."
        if include_expired else
        "Only include active (non-expired) deals."
    )
    try:
        result = sgai.extract(
            url=url,
            schema=DealmoonSchema.model_json_schema(),
            prompt=(
                "Extract all deals listed on the page. For each deal, capture: "
                "(1) the title/headline, "
                "(2) the full description including terms, promo codes, or gift details, "
                "(3) the date the deal was posted or started — look for timestamps or date "
                "labels near the deal entry (return as a string like 'May 30' or '2026-05-30', "
                "or empty string if not visible), "
                f"(4) whether it is marked as expired. {expired_instruction}"
            ),
            fetch_config=FetchConfig(mode="js", stealth=True, wait=4000),
        )

        # Defensive status check — never trust result.data without verifying status first
        if result.status != "success":
            print(f"      ⚠️  SDK error status: {result.error}")
            return None

        # Defensive dict access — ExtractResponse.json_data is the authoritative container
        data = result.data.json_data
        if not data or not isinstance(data, dict):
            print("      ⚠️  Extraction succeeded but json_data is empty or malformed.")
            return None

        return data

    except Exception as exc:
        print(f"      ⚠️  Exception during extraction: {exc}")
        return None


def try_extract_brand(
    sgai: ScrapeGraphAI,
    brand_cfg: dict,
    include_expired: bool,
    label: str = "",
) -> list[dict] | None:
    """
    Attempt extraction for one brand up to MAX_RETRIES times on the primary URL.
    No fallback URL — the official-store sid-filtered URLs are the only valid source.
    Returns list of raw deal dicts, or None if all attempts fail.
    """
    brand = brand_cfg["brand"]
    url   = _build_url(brand_cfg["url"], include_expired)
    tag   = f" [{label}]" if label else ""

    print(f"\n→ {brand}{tag}")
    print(f"  URL: {url}")

    for attempt in range(1, MAX_RETRIES + 1):
        print(f"  Attempt {attempt}/{MAX_RETRIES}...")
        data = _extract_once(sgai, url, include_expired)
        if data is not None:
            deals = data.get("deals", [])
            print(f"  ✅ {len(deals)} deal(s) extracted.")
            return deals

        if attempt < MAX_RETRIES:
            delay = RETRY_DELAYS[attempt - 1]
            print(f"  Waiting {delay}s before retry...")
            time.sleep(delay)

    print(f"  ❌ All {MAX_RETRIES} attempts failed for {brand}.")
    return None


# ── Orchestration ──────────────────────────────────────────────────────────────

def run_extraction_flow(
    sgai: ScrapeGraphAI,
    active_brands: list[dict],
    include_expired: bool,
) -> dict[str, list[dict] | None]:
    """
    Full extraction flow with Lancome-priority retry logic:

    1. Extract all brands in config order (Lancome first), each up to 3 attempts.
    2. If Lancome failed AND at least one other brand succeeded → retry Lancome
       up to 3 more times (Lancome has higher priority).
    3. Return a dict: brand_name → deals list (or None on failure).
    """
    results: dict[str, list[dict] | None] = {}

    # Step 1 — Extract all brands in config order
    for brand_cfg in active_brands:
        results[brand_cfg["brand"]] = try_extract_brand(sgai, brand_cfg, include_expired)

    # Step 2 — Lancome-priority retry: if Lancome failed but another brand succeeded
    lancome_cfg = next((b for b in active_brands if b["brand"] == "Lancome"), None)
    if lancome_cfg and results.get("Lancome") is None:
        other_succeeded = any(
            v is not None for k, v in results.items() if k != "Lancome"
        )
        if other_succeeded:
            print("\n  ↩️  Lancome failed but other brand(s) succeeded — retrying Lancome...")
            results["Lancome"] = try_extract_brand(
                sgai, lancome_cfg, include_expired, label="priority retry"
            )

    return results


def process_results(
    results: dict[str, list[dict] | None],
    active_brands: list[dict],
    state: dict,
) -> bool:
    """
    Apply keyword filtering and send ntfy alerts for any new active BOGO matches.
    Expired deals are only processed when INCLUDE_EXPIRED=True (E2E test mode).
    Returns True if state was modified (i.e., at least one new alert was sent).
    """
    state_modified = False
    brand_map      = {b["brand"]: b for b in active_brands}

    for brand_name, deals in results.items():
        if deals is None:
            continue

        brand_cfg = brand_map[brand_name]
        keywords  = [kw.lower() for kw in brand_cfg.get("keywords", [])]

        print(f"\n  Processing {brand_name} ({len(deals)} deal(s))...")

        for deal in deals:
            # Defensive .get() access — never assume all schema fields are present
            title       = deal.get("title", "").strip()
            description = deal.get("description", "").strip()
            deal_date   = deal.get("deal_date", "").strip()

            # Dual expired check: AI field AND title prefix.
            # Dealmoon always prefixes expired deal titles with "[Expired]" on the
            # server side. This is a reliable marker that does not depend on AI
            # classification accuracy. Using both catches cases where the AI
            # incorrectly returns is_expired=False for a visibly expired deal.
            ai_expired    = deal.get("is_expired", False)
            title_expired = title.lower().startswith("[expired]")
            is_expired    = ai_expired or title_expired

            if not title:
                continue

            # Skip expired deals in normal daily runs
            if is_expired and not INCLUDE_EXPIRED:
                continue

            # Case-insensitive keyword match across title + description
            combined   = f"{title} {description}".lower()
            matched_kw = next((kw for kw in keywords if kw in combined), None)
            if not matched_kw:
                continue

            status = "EXPIRED" if is_expired else "ACTIVE"
            print(f"\n    🎯 BOGO match! Keyword: '{matched_kw}'")
            print(f"       Title:  {title}")
            print(f"       Date:   {deal_date or 'N/A'}")
            print(f"       Status: {status}")

            if already_notified(state, title):
                print("       ℹ️  Already notified — skipping duplicate alert.")
                continue

            notify_bogo_deal(brand_name, title, description, deal_date)
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

    # Load brand configuration
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        all_brands = json.load(f)

    active_brands = [b for b in all_brands if b.get("active", True)]
    if not active_brands:
        print("No active brands in deals_config.json. Nothing to check.")
        sys.exit(0)

    brand_names = [b["brand"] for b in active_brands]
    print(f"🔍 Checking {len(active_brands)} brand(s): {', '.join(brand_names)}\n")

    state = load_state()
    sgai  = ScrapeGraphAI(api_key=SGAI_API_KEY)

    # ── First extraction flow ──────────────────────────────────────────────────
    print("── Extraction flow (attempt 1) ──")
    results = run_extraction_flow(sgai, active_brands, INCLUDE_EXPIRED)

    # ── Full-flow retry on total failure ───────────────────────────────────────
    if all(v is None for v in results.values()):
        wait_hours = FLOW_RETRY_WAIT // 3600
        print(f"\n⏳ All brands failed on first flow. Retrying entire flow in {wait_hours}h...")
        time.sleep(FLOW_RETRY_WAIT)

        print("\n── Extraction flow (attempt 2 — after retry wait) ──")
        results = run_extraction_flow(sgai, active_brands, INCLUDE_EXPIRED)

        if all(v is None for v in results.values()):
            print("\n❌ Retry flow also failed. Sending error notification.")
            notify_extraction_error(brand_names)
            sys.exit(1)

    # ── Process results — filter + alert ──────────────────────────────────────
    state_modified = process_results(results, active_brands, state)

    if state_modified:
        save_state(state)
        print("\n💾 State saved to deals_state.json.")

    print("\n✅ All checks complete.")


if __name__ == "__main__":
    main()
