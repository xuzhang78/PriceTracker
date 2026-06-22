"""
B&H Photo MacBook Pro M5 Price Tracker
---------------------------------------
Reads tracker_config.json (type: bh_price), uses AI extraction to get the
current price, and sends an ntfy.sh push notification when price drops below
the configured threshold.

Extraction provider selection (automatic):
  1. Check SGAI credit balance. If credits >= cost of one full run (10 per
     product), use ScrapeGraphAI.
  2. If SGAI credits are exhausted or below threshold, fall back to Firecrawl.

Run modes:
  Normal run:       python check_prices.py
  Report mode:      REPORT_MODE=true python check_prices.py
                    → real extraction, always notify with actual price
  Simulate alert:   PRICE_OVERRIDE=2499.00 python check_prices.py
                    → skips real extraction, tests notification pipeline
"""

import json
import os
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

import requests
from dotenv import load_dotenv
from pydantic import BaseModel

# ── Configuration ──────────────────────────────────────────────────────────────

load_dotenv()

SGAI_API_KEY        = os.getenv("SGAI_API_KEY", "")
FIRECRAWL_API_KEY   = os.getenv("FIRECRAWL_API_KEY", "")
NTFY_TOPIC          = os.getenv("NTFY_TOPIC", "")
_price_override_raw = os.getenv("PRICE_OVERRIDE", "").strip()
PRICE_OVERRIDE: float | None = float(_price_override_raw) if _price_override_raw else None
REPORT_MODE         = os.getenv("REPORT_MODE", "false").strip().lower() == "true"
TESTING             = os.getenv("TESTING", "false").strip().lower() == "true"
IS_TEST             = (PRICE_OVERRIDE is not None) or REPORT_MODE or TESTING

SGAI_CREDITS_PER_CALL = 10       # credits consumed per SGAI extract call
FLOW_RETRY_WAIT       = 2 * 3600

# Prices outside this range indicate a failed / blocked extraction
MIN_VALID_PRICE = 100.0
MAX_VALID_PRICE = 10_000.0

BASE_DIR     = os.path.dirname(__file__)
CONFIG_PATH  = os.path.join(BASE_DIR, "tracker_config.json")


# ── Data Schema ────────────────────────────────────────────────────────────────

class MacBookProductSchema(BaseModel):
    """Structured fields extracted by AI from each product page."""
    product_title: str
    current_price: float   # USD, numeric only — no $ or commas
    stock_status:  str


# ── Notification Helpers ───────────────────────────────────────────────────────

def _send_ntfy(title: str, body: str, priority: str = "default", tags: str = "") -> None:
    """POST a push notification to ntfy.sh with ASCII-safe header, up to 3 attempts."""
    safe_title = title.encode("ascii", errors="replace").decode("ascii")
    for attempt in range(1, 4):
        try:
            response = requests.post(
                f"https://ntfy.sh/{NTFY_TOPIC}",
                data=body.encode("utf-8"),
                headers={"Title": safe_title, "Priority": priority, "Tags": tags},
                timeout=10,
            )
            response.raise_for_status()
            return
        except requests.RequestException as exc:
            print(f"    ⚠️  ntfy send attempt {attempt}/3 failed: {exc}")
            if attempt < 3:
                time.sleep(5)


def notify_price_drop(product_title: str, price: float, stock_status: str, url: str) -> None:
    """Send a high-priority price-drop alert."""
    _send_ntfy(
        title="B&H MacBook Price Drop!",
        body=f"Price dropped to ${price:.2f}\n{product_title}\nStock: {stock_status}\n{url}",
        priority="high",
        tags="rotating_light,apple,moneybag",
    )
    print("    ✅ Price drop alert sent via ntfy!")


def notify_price_report(product_title: str, price: float, threshold: float,
                        stock_status: str, url: str) -> None:
    """Send a report-mode notification showing the real extracted price vs threshold."""
    gap = price - threshold
    vs_threshold = f"${gap:+.2f} vs threshold ${threshold:.2f}"
    _send_ntfy(
        title="[Report] B&H Price Check",
        body=f"Current price: ${price:.2f}\n{vs_threshold}\n{product_title}\nStock: {stock_status}\n{url}",
        priority="default",
        tags="chart_with_upwards_trend,apple",
    )
    print(f"    📊 Price report sent via ntfy! (${price:.2f}, {vs_threshold})")


def notify_extraction_error(product_name: str, reason: str) -> None:
    """Send a warning alert when extraction fails or sanity check fails."""
    _send_ntfy(
        title="Price Tracker — Extraction Error",
        body=f"Failed to extract price for:\n{product_name}\n\nReason: {reason}",
        priority="default",
        tags="warning",
    )
    print("    ⚠️  Error alert sent via ntfy!")


# ── Provider Selection (mirrors check_deals.py) ────────────────────────────────

def _check_sgai_credits() -> int:
    """
    Returns remaining SGAI credits as an integer.
    Returns -1 if the check itself fails (network error, invalid key, etc.).
    Returns  0 if credits are confirmed exhausted.
    """
    if not SGAI_API_KEY:
        return -1
    try:
        from scrapegraph_py import ScrapeGraphAI
        sgai = ScrapeGraphAI(api_key=SGAI_API_KEY)
        info = sgai.credits()
        # Handle nested data attribute first (new SDK format)
        if hasattr(info, "data") and info.data is not None:
            d = info.data
            for field in ("remaining", "remaining_credits", "credits", "balance"):
                if hasattr(d, field):
                    return int(getattr(d, field))
                if isinstance(d, dict) and field in d:
                    return int(d[field])
        if isinstance(info, (int, float)):
            return int(info)
        if isinstance(info, dict):
            for field in ("remaining", "remaining_credits", "credits", "balance"):
                if field in info:
                    return int(info[field])
        for attr in ("remaining", "remaining_credits", "credits", "balance"):
            if hasattr(info, attr):
                return int(getattr(info, attr))
        print(f"  ⚠️  Unrecognised SGAI credits response: {info}")
        return -1
    except Exception as exc:
        print(f"  ⚠️  SGAI credit check failed: {exc}")
        return -1


def select_provider(min_credits_needed: int) -> tuple[str, object]:
    """
    Selects extraction provider based on SGAI credit availability.
      1. SGAI  — if credit balance >= min_credits_needed
      2. Firecrawl — automatic fallback when SGAI credits are insufficient
    Returns (provider_name, client). Exits with code 1 if neither is available.
    """
    print("🔌 Selecting extraction provider...")

    remaining = _check_sgai_credits()

    if remaining == -1:
        if SGAI_API_KEY:
            print("  ⚠️  SGAI credit check inconclusive — attempting SGAI anyway.")
            from scrapegraph_py import ScrapeGraphAI
            return "sgai", ScrapeGraphAI(api_key=SGAI_API_KEY)
    elif remaining >= min_credits_needed:
        print(f"  ✅ SGAI: {remaining} credits available (need {min_credits_needed}) → using SGAI")
        from scrapegraph_py import ScrapeGraphAI
        return "sgai", ScrapeGraphAI(api_key=SGAI_API_KEY)
    else:
        print(f"  ⚠️  SGAI: only {remaining} credits remaining (need {min_credits_needed})")

    if FIRECRAWL_API_KEY:
        print("  ✅ Falling back to Firecrawl.")
        from firecrawl import FirecrawlApp
        return "firecrawl", FirecrawlApp(api_key=FIRECRAWL_API_KEY)

    print("❌ No extraction provider available.")
    print("   • SGAI: credits exhausted or key missing → set SGAI_API_KEY")
    print("   • Firecrawl: not configured → set FIRECRAWL_API_KEY")
    sys.exit(1)


# ── Extraction ─────────────────────────────────────────────────────────────────

_PRICE_PROMPT = (
    "Extract the product title, the current retail price in USD as a plain "
    "float (no currency symbols, no commas), and the stock availability status."
)

MAX_RETRIES   = 1 if IS_TEST else 3
RETRY_DELAYS  = [15, 30, 60]


def parse_bh_product_html(html: str) -> dict | None:
    """Parses B&H product HTML using data-selenium attributes."""
    import re
    import html as html_lib

    # 1. Product Title (data-selenium="productTitle" on h1)
    title_match = re.search(r'data-selenium="productTitle"[^>]*>(.*?)</h1>', html, re.DOTALL)
    title = title_match.group(1).strip() if title_match else ""

    # 2. Stock Status (data-selenium="stockStatus" on span)
    stock_match = re.search(r'data-selenium="stockStatus"[^>]*>(.*?)</span>', html, re.DOTALL)
    stock = stock_match.group(1).strip() if stock_match else ""

    # 3. Current Price (data-selenium="pricingPrice" on span)
    price_match = re.search(r'data-selenium="pricingPrice"[^>]*>(.*?)</span>', html, re.DOTALL)
    price = 0.0
    if price_match:
        price_str = price_match.group(1).strip()
        # Clean currency symbols and commas
        price_str = re.sub(r'[^\d.]', '', price_str)
        try:
            price = float(price_str)
        except ValueError:
            pass

    # Fallback to standard h1 tag if data-selenium title is missing
    if not title:
        fallback_h1 = re.search(r'<h1[^>]*>(.*?)</h1>', html, re.DOTALL)
        if fallback_h1:
            title = fallback_h1.group(1).strip()

    # Clean HTML tags and entities
    if title:
        title = html_lib.unescape(title)
        title = re.sub(r'<[^>]*>', '', title)
        title = title.strip()

    if stock:
        stock = html_lib.unescape(stock)
        stock = re.sub(r'<[^>]*>', '', stock)
        stock = stock.strip()

    if not title:
        return None

    return {
        "product_title": title,
        "current_price": price,
        "stock_status": stock or "Unknown"
    }


def _extract_price_once(provider: str, client, url: str) -> dict | None:
    """Perform a single-attempt extraction using either ScrapeGraphAI or Firecrawl."""
    html_content = None

    if provider == "sgai":
        from scrapegraph_py import FetchConfig, HtmlFormatConfig
        try:
            print("      🔍 SGAI: Fetching raw HTML (1 credit)...")
            result = client.scrape(
                url=url,
                formats=[HtmlFormatConfig(mode="normal")],
                fetch_config=FetchConfig(mode="js", stealth=True, wait=2000),
            )
            if result.status == "error":
                print(f"      ⚠️  SGAI html scrape error: {result.error}")
            elif hasattr(result, "data") and result.data is not None:
                results_dict = getattr(result.data, "results", {})
                html_dict = results_dict.get("html", {})
                if isinstance(html_dict, dict):
                    html_content = html_dict.get("data")
                else:
                    html_content = html_dict
        except Exception as exc:
            print(f"      ⚠️  SGAI html scrape exception: {exc}")
    else:
        try:
            print("      🔍 Firecrawl: Fetching raw HTML (1 credit)...")
            doc = client.scrape(
                url,
                formats=["html"]
            )
            if hasattr(doc, "html") and doc.html:
                html_content = doc.html
            elif isinstance(doc, dict) and "html" in doc:
                html_content = doc["html"]
        except Exception as exc:
            print(f"      ⚠️  Firecrawl html scrape exception: {exc}")

    # Parse HTML using regex
    if html_content:
        parsed = parse_bh_product_html(html_content)
        if parsed and parsed.get("product_title") and parsed.get("current_price") > 0.0:
            print(f"      🎯 {provider.upper()}: Successfully parsed product details via HTML regex (Save successful!).")
            return parsed
        else:
            print(f"      ⚠️  {provider.upper()}: Regex parsing returned incomplete data (Title: {parsed.get('product_title') if parsed else 'None'}, Price: {parsed.get('current_price') if parsed else 'None'}).")
    else:
        print(f"      ⚠️  {provider.upper()}: Failed to fetch HTML content.")

    # Fallback to LLM structured extraction if html-only scrape or regex parsing failed
    print(f"      🔄 {provider.upper()}: Falling back to LLM structured extraction...")
    if provider == "sgai":
        from scrapegraph_py import FetchConfig
        result = client.extract(
            url=url,
            schema=MacBookProductSchema.model_json_schema(),
            prompt=_PRICE_PROMPT,
            fetch_config=FetchConfig(mode="js", stealth=True, wait=2000),
        )
        if result.status == "error":
            print(f"      ⚠️  SGAI LLM fallback error: {result.error}")
            return None
        json_data = result.data.json_data
        if not json_data:
            print(f"      ⚠️  SGAI: LLM fallback json_data empty or malformed.")
            return None
        return json_data
    else:
        doc = client.scrape(
            url,
            formats=[
                {
                    "type": "json",
                    "schema": MacBookProductSchema.model_json_schema(),
                    "prompt": _PRICE_PROMPT
                }
            ]
        )
        if hasattr(doc, "json") and doc.json is not None:
            if isinstance(doc.json, dict) and "current_price" in doc.json:
                return doc.json
        print("      ⚠️  Firecrawl: LLM fallback 'json' field empty or malformed.")
        return None


def try_extract_product(
    provider: str,
    client,
    product: dict,
    label: str = "",
) -> dict | None:
    """
    Attempt price extraction for a single product up to MAX_RETRIES times.
    Returns the extracted dict, or None if all attempts fail.
    """
    name  = product["name"]
    url   = product["url"]
    tag   = f" [{label}]" if label else ""

    print(f"\n→ {name}{tag} [{provider.upper()}]")
    print(f"  URL: {url}")

    for attempt in range(1, MAX_RETRIES + 1):
        print(f"  Attempt {attempt}/{MAX_RETRIES}...")
        try:
            data = _extract_price_once(provider, client, url)
            if data is not None:
                return data
        except Exception as exc:
            print(f"      ⚠️  {provider.upper()} exception: {exc}")

        if attempt < MAX_RETRIES:
            delay = RETRY_DELAYS[attempt - 1]
            print(f"  Waiting {delay}s before retry...")
            time.sleep(delay)

    print(f"  ❌ All {MAX_RETRIES} attempts failed for {name} via {provider.upper()}.")
    return None


def load_active_products() -> list[dict]:
    """Load tracker_config.json and return only bh_price entries where active == true."""
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)
    all_products = [t for t in config.get("trackers", []) if t.get("type") == "bh_price"]
    return [p for p in all_products if p.get("active", False)]


def extract_product_data(provider: str, client, product: dict) -> dict | None:
    """
    Extracts price and stock data for one product.
    Returns extracted data dict or None on failure.
    """
    name: str = product["name"]

    # ── Simulation mode ────────────────────────────────────────────────────────
    if PRICE_OVERRIDE is not None:
        return {
            "product_title": name,
            "current_price": PRICE_OVERRIDE,
            "stock_status": "Simulated"
        }

    # Try primary provider
    res = try_extract_product(provider, client, product)
    if res is not None:
        return res

    # Only allow fallback from SGAI to Firecrawl. If primary is Firecrawl, do not fallback to SGAI.
    if provider == "firecrawl":
        return None

    secondary_provider = "firecrawl"
    secondary_key = FIRECRAWL_API_KEY

    if secondary_key:
        print(f"\n🔄 Primary [{provider.upper()}] failed for {name}. Falling back to [{secondary_provider.upper()}]...")
        try:
            if secondary_provider == "firecrawl":
                from firecrawl import FirecrawlApp
                sec_client = FirecrawlApp(api_key=FIRECRAWL_API_KEY)
            else:
                from scrapegraph_py import ScrapeGraphAI
                sec_client = ScrapeGraphAI(api_key=SGAI_API_KEY)

            res = try_extract_product(secondary_provider, sec_client, product, "fallback")
            if res is not None:
                return res
        except Exception as exc:
            print(f"  ⚠️  Failed to initialize or extract via fallback provider {secondary_provider.upper()}: {exc}")

    return None


def process_product_result(product: dict, json_data: dict | None) -> None:
    """Validate extracted product data and trigger notifications based on thresholds."""
    name:      str   = product["name"]
    url:       str   = product["url"]
    threshold: float = float(product["threshold"])

    print(f"→ {name}")
    print(f"  URL: {url}")
    print(f"  Threshold: ${threshold:.2f}")

    if json_data is None:
        notify_extraction_error(name, "All extraction attempts failed.")
        return

    try:
        price         = float(json_data["current_price"])
        product_title = str(json_data["product_title"])
        stock_status  = str(json_data["stock_status"])
    except (KeyError, TypeError, ValueError) as exc:
        msg = f"Field read error: {exc} | json_data: {json_data}"
        print(f"  ❌ {msg}")
        notify_extraction_error(name, msg)
        return

    print(f"  Title:  {product_title}")
    print(f"  Stock:  {stock_status}")
    print(f"  Price:  ${price:.2f}")

    # ── Out of Stock Check ─────────────────────────────────────────────────────
    stock_status_lower = stock_status.lower()
    is_out_of_stock = (
        "out of stock" in stock_status_lower or
        "email when" in stock_status_lower or
        "notify" in stock_status_lower or
        "coming soon" in stock_status_lower or
        "unavailable" in stock_status_lower
    )
    if is_out_of_stock and price < MIN_VALID_PRICE:
        print("  ℹ️ Product is Out of Stock. Skipping price check and sanity guard.")
        return

    # ── Sanity guard ───────────────────────────────────────────────────────────
    if not (MIN_VALID_PRICE < price < MAX_VALID_PRICE):
        reason = (
            f"Extracted price ${price:.2f} is outside the valid range "
            f"(${MIN_VALID_PRICE:.0f}–${MAX_VALID_PRICE:,.0f}). "
            "Possible Cloudflare block or page layout change."
        )
        print(f"  ❌ Sanity check failed — {reason}")
        notify_extraction_error(name, reason)
        return

    # ── Notification ───────────────────────────────────────────────────────────
    display_title = product_title if PRICE_OVERRIDE is None else name

    if REPORT_MODE:
        print("  📊 REPORT MODE — sending price report regardless of threshold.")
        notify_price_report(display_title, price, threshold, stock_status, url)
    elif price < threshold:
        print("  🚨 BELOW THRESHOLD — sending alert!")
        notify_price_drop(display_title, price, stock_status, url)
    else:
        gap = price - threshold
        print(f"  ✅ Above threshold by ${gap:.2f} — no alert.")


def run_extraction_flow(provider: str, client, products: list[dict]) -> dict[str, dict | None]:
    """Execute extraction for all products and collect results."""
    results = {}
    for product in products:
        results[product["name"]] = extract_product_data(provider, client, product)
    return results


# ── Entry Point ────────────────────────────────────────────────────────────────

def main() -> None:
    if not NTFY_TOPIC:
        print("❌ Missing NTFY_TOPIC. Set it in .env or GitHub Secrets.")
        sys.exit(1)
    if not SGAI_API_KEY and not FIRECRAWL_API_KEY:
        print("❌ No extraction provider configured.")
        print("   Set SGAI_API_KEY and/or FIRECRAWL_API_KEY in .env or GitHub Secrets.")
        sys.exit(1)

    if REPORT_MODE:
        print("📊 Running in REPORT MODE — real extraction, always notify with actual price.\n")
    if PRICE_OVERRIDE is not None:
        print(f"⚠️  Running in SIMULATION mode — PRICE_OVERRIDE=${PRICE_OVERRIDE:.2f}\n")

    products = load_active_products()
    if not products:
        print("No active bh_price entries in tracker_config.json. Nothing to check.")
        sys.exit(0)

    print(f"🔍 Checking {len(products)} active product(s).\n")

    min_credits = SGAI_CREDITS_PER_CALL * len(products)
    provider, client = select_provider(min_credits)
    print()

    # ── First extraction flow ──────────────────────────────────────────────────
    print("── Extraction flow (attempt 1) ──")
    results = run_extraction_flow(provider, client, products)

    # ── Full-flow retry on total failure ───────────────────────────────────────
    if all(v is None for v in results.values()):
        if IS_TEST:
            print("\n⚠️ All products failed, but running in testing mode — skipping 2-hour retry sleep.")
            for product in products:
                notify_extraction_error(product["name"], "All extraction attempts failed (test mode).")
            sys.exit(1)

        wait_hours = FLOW_RETRY_WAIT // 3600
        print(f"\n⏳ All products failed. Retrying entire flow in {wait_hours}h...")
        time.sleep(FLOW_RETRY_WAIT)

        print("\n── Extraction flow (attempt 2 — after retry wait) ──")
        results = run_extraction_flow(provider, client, products)

        if all(v is None for v in results.values()):
            print("\n❌ Retry flow also failed. Sending error notifications.")
            for product in products:
                notify_extraction_error(product["name"], "All extraction attempts failed.")
            sys.exit(1)

    # ── Process results (validate and notify) ──────────────────────────────────
    for product in products:
        json_data = results.get(product["name"])
        process_product_result(product, json_data)
        print()

    print("✅ All checks complete.")


if __name__ == "__main__":
    main()
