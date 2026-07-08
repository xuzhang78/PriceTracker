"""
Product Price Tracker
---------------------
Reads tracker_config.json and tracks prices for:
  - bh_price:     B&H Photo products (HTML regex + LLM fallback)
  - iphone_price: iPhone products on BestBuy / Verizon (LLM-only extraction)

Sends an ntfy.sh push notification when price drops below the configured
threshold. Supports a start_date field to delay tracking until a future date.

Extraction provider hierarchy:
  1. Primary:   Firecrawl (LLM structured extraction with JS rendering)
  2. Secondary: ScraperAPI proxy + Google Gemini LLM
  3. Tertiary:  Scrape.do proxy + Google Gemini LLM

Run modes:
  Normal run:       python check_prices.py
  Report mode:      REPORT_MODE=true python check_prices.py
                    → real extraction, always notify with actual price
  Simulate alert:   PRICE_OVERRIDE=2499.00 python check_prices.py
                    → skips real extraction, tests notification pipeline
"""

import datetime
import json
import os
import re
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

import requests
from google import genai
from dotenv import load_dotenv
from pydantic import BaseModel
from utils import send_ntfy, select_provider, clean_html

# ── Configuration ──────────────────────────────────────────────────────────────

load_dotenv()

FIRECRAWL_API_KEY   = os.getenv("FIRECRAWL_API_KEY", "")
SCRAPERAPI_KEY      = os.getenv("SCRAPERAPI_KEY", "")
SCRAPE_DO_KEY       = os.getenv("SCRAPE_DO_KEY", "")
GEMINI_API_KEY      = os.getenv("GEMINI_API_KEY", "")
NTFY_TOPIC          = os.getenv("NTFY_TOPIC", "")
_price_override_raw = os.getenv("PRICE_OVERRIDE", "").strip()
PRICE_OVERRIDE: float | None = float(_price_override_raw) if _price_override_raw else None
REPORT_MODE         = os.getenv("REPORT_MODE", "false").strip().lower() == "true"
TESTING             = os.getenv("TESTING", "false").strip().lower() == "true"
IS_TEST             = (PRICE_OVERRIDE is not None) or REPORT_MODE or TESTING

FLOW_RETRY_WAIT       = 2 * 3600

# Prices outside this range indicate a failed / blocked extraction
MIN_VALID_PRICE = 100.0
MAX_VALID_PRICE = 10_000.0

BASE_DIR     = os.path.dirname(__file__)
CONFIG_PATH  = os.path.join(BASE_DIR, "tracker_config.json")


# ── Data Schemas ───────────────────────────────────────────────────────────────

class MacBookProductSchema(BaseModel):
    """Structured fields extracted by AI from each product page."""
    product_title: str
    current_price: float   # USD, numeric only — no $ or commas
    stock_status:  str


class IPhoneProductSchema(BaseModel):
    """Structured fields extracted by AI from iPhone product pages."""
    product_title: str
    current_price: float   # USD single payment, no $ or commas
    stock_status:  str


# ── Notification Helpers ───────────────────────────────────────────────────────

def _ntfy_title_for_type(product_type: str, prefix: str) -> str:
    """Return a contextual ntfy title based on tracker type."""
    if product_type == "iphone_price":
        return f"{prefix}iPhone Price Drop!"
    return f"{prefix}B&H MacBook Price Drop!"


def notify_price_drop(product_title: str, price: float, stock_status: str,
                      url: str, product_type: str = "bh_price") -> None:
    """Send a high-priority price-drop alert."""
    send_ntfy(
        title=_ntfy_title_for_type(product_type, ""),
        body=f"Price dropped to ${price:.2f}\n{product_title}\nStock: {stock_status}\n{url}",
        priority="high",
        tags="rotating_light,apple,moneybag",
    )
    print("    ✅ Price drop alert sent via ntfy!")


def notify_price_report(product_title: str, price: float, threshold: float,
                        stock_status: str, url: str,
                        product_type: str = "bh_price") -> None:
    """Send a report-mode notification showing the real extracted price vs threshold."""
    gap = price - threshold
    vs_threshold = f"${gap:+.2f} vs threshold ${threshold:.2f}"
    report_label = "iPhone" if product_type == "iphone_price" else "B&H"
    send_ntfy(
        title=f"[Report] {report_label} Price Check",
        body=f"Current price: ${price:.2f}\n{vs_threshold}\n{product_title}\nStock: {stock_status}\n{url}",
        priority="default",
        tags="chart_with_upwards_trend,apple",
    )
    print(f"    📊 Price report sent via ntfy! (${price:.2f}, {vs_threshold})")


def notify_extraction_error(product_name: str, reason: str) -> None:
    """Send a warning alert when extraction fails or sanity check fails."""
    send_ntfy(
        title="Price Tracker — Extraction Error",
        body=f"Failed to extract price for:\n{product_name}\n\nReason: {reason}",
        priority="default",
        tags="warning",
    )
    print("    ⚠️  Error alert sent via ntfy!")


# ── Extraction ─────────────────────────────────────────────────────────────────

_PRICE_PROMPT = (
    "Extract the product title, the current retail price in USD as a plain "
    "float (no currency symbols, no commas), and the stock availability status. "
    "For stock status, extract the shipping availability status (specifically whether it is available for shipping, ignoring local store pickup availability)."
)

_IPHONE_PRICE_PROMPT = (
    "Extract the product title, the current full retail price (single "
    "payment, not monthly installment) in USD as a plain float (no currency "
    "symbols, no commas), and the stock availability status. "
    "Important: return the one-time purchase price, NOT a monthly payment. "
    "If the page shows multiple storage sizes or variants (e.g. 256GB, 512GB, 1TB), "
    "specifically extract the price for the 512GB variant. "
    "For Verizon: look for the 512GB storage model (SKU 'sku6037331') in the page content or embedded catalog JSON script tags "
    "and extract its full purchase price (usually $1399.99). "
    "For stock status: extract the shipping availability status (specifically whether it is available for shipping, ignoring local store pickup availability)."
)

MAX_RETRIES   = 1 if IS_TEST else 3
RETRY_DELAYS  = [15, 30, 60]


def parse_bh_product_html(html: str) -> dict | None:
    """Parses B&H product HTML using data-selenium attributes."""
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


def _extract_price_once(client, url: str) -> dict | None:
    """Perform a single-attempt B&H price extraction via Firecrawl.

    Strategy: fetch raw HTML first and attempt regex parsing (zero LLM cost).
    If regex fails, fall back to Firecrawl LLM structured extraction.
    """
    html_content = None

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
            print(f"      🎯 FIRECRAWL: Successfully parsed product details via HTML regex (Save successful!).")
            return parsed
        else:
            print(f"      ⚠️  FIRECRAWL: Regex parsing returned incomplete data (Title: {parsed.get('product_title') if parsed else 'None'}, Price: {parsed.get('current_price') if parsed else 'None'}).")
    else:
        print(f"      ⚠️  FIRECRAWL: Failed to fetch HTML content.")

    # Fallback to LLM structured extraction if html-only scrape or regex parsing failed
    print(f"      🔄 FIRECRAWL: Falling back to LLM structured extraction...")
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


def _extract_iphone_price_once(client, url: str) -> dict | None:
    """LLM-only extraction for iPhone product pages (BestBuy, Verizon).

    Unlike B&H products, these sites have no stable data-selenium attributes
    for regex parsing, so we go straight to LLM structured extraction.
    """

    schema = IPhoneProductSchema.model_json_schema()
    prompt = _IPHONE_PRICE_PROMPT

    try:
        print(f"      🔍 Firecrawl: LLM structured extraction for iPhone (URL: {url})...")
        scrape_params = {
            "formats": [
                {
                    "type": "json",
                    "schema": schema,
                    "prompt": prompt
                }
            ]
        }
        
        # Click the 512GB option on Verizon before extracting price
        if "verizon.com" in url:
            scrape_params["actions"] = [
                {"type": "click", "selector": "text=512 GB"},
                {"type": "wait", "milliseconds": 2000}
            ]
            
        # Give BestBuy plenty of time due to anti-bot timeouts
        if "bestbuy.com" in url:
            scrape_params["timeout"] = 120000

        doc = client.scrape(
            url,
            **scrape_params
        )
        if hasattr(doc, "json") and doc.json is not None:
            if isinstance(doc.json, dict) and "current_price" in doc.json:
                return doc.json
        print("      ⚠️  Firecrawl: 'json' field empty or malformed.")
        return None
    except Exception as exc:
        print(f"      ⚠️  Firecrawl exception: {exc}")
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
    if client is None:
        return None

    name  = product["name"]
    url   = product["url"]
    ptype = product.get("type", "bh_price")
    tag   = f" [{label}]" if label else ""

    # Select extraction function based on tracker type
    extract_fn = _extract_iphone_price_once if ptype == "iphone_price" else _extract_price_once

    print(f"\n→ {name}{tag} [{provider.upper()}]")
    print(f"  URL: {url}")

    for attempt in range(1, MAX_RETRIES + 1):
        print(f"  Attempt {attempt}/{MAX_RETRIES}...")
        try:
            data = extract_fn(client, url)
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
    """Load tracker_config.json and return active bh_price + iphone_price entries.

    Respects the optional start_date field: entries with a future start_date
    are skipped until that date arrives.
    """
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)
    valid_types = ("bh_price", "iphone_price")
    all_products = [t for t in config.get("trackers", []) if t.get("type") in valid_types]
    today = datetime.date.today()
    active = []
    for p in all_products:
        if not p.get("active", False):
            continue
        start_date_str = p.get("start_date")
        if start_date_str:
            if IS_TEST:
                print(f"  🧪 Testing mode: Bypassing start_date ({start_date_str}) for '{p['name']}'")
            else:
                start = datetime.date.fromisoformat(start_date_str)
                if today < start:
                    print(f"  ⏳ Skipping '{p['name']}' — starts on {start_date_str}")
                    continue
        active.append(p)
    return active


def _extract_via_proxy_and_gemini(provider: str, proxy_key: str, url: str, schema: dict, prompt: str) -> dict | None:
    """Fetch HTML using ScraperAPI or Scrape.do proxy, and extract structured JSON using Gemini LLM."""
    if not GEMINI_API_KEY:
        print("      ⚠️  Gemini: GEMINI_API_KEY not configured. Cannot perform LLM extraction.")
        return None

    html_content = ""
    try:
        if provider == "scraperapi":
            print(f"      🔍 ScraperAPI proxy: Fetching HTML for {url}...")
            params = {'api_key': proxy_key, 'url': url}
            if "bestbuy" in url or "bhphotovideo" in url:
                params['render'] = 'true'
            r = requests.get('https://api.scraperapi.com/', params=params, timeout=90)
            if r.status_code == 200:
                html_content = r.text
            else:
                print(f"      ⚠️  ScraperAPI: Returned status code {r.status_code}")
                return None
        elif provider == "scrapedo":
            print(f"      🔍 Scrape.do proxy: Fetching HTML for {url}...")
            params = {'token': proxy_key, 'url': url}
            if "bestbuy" in url or "bhphotovideo" in url:
                params['render'] = 'true'
            r = requests.get('https://api.scrape.do', params=params, timeout=90)
            if r.status_code == 200:
                html_content = r.text
            else:
                print(f"      ⚠️  Scrape.do: Returned status code {r.status_code}")
                return None
        else:
            return None
    except Exception as e:
        print(f"      ⚠️  Proxy fetch exception via {provider.upper()}: {e}")
        return None

    if not html_content:
        print(f"      ⚠️  Proxy fetch via {provider.upper()} returned empty content.")
        return None

    try:
        print("      🧠 Gemini: Extracting structured data from retrieved HTML...")
        from google.genai import types
        
        # Clean HTML to minimize tokens and prevent truncation
        cleaned_html = clean_html(html_content, keep_catalog_scripts=True)
        
        # Initialize unified Client
        client = genai.Client(api_key=GEMINI_API_KEY)
        
        full_prompt = (
            f"HTML Content:\n"
            f"```html\n{cleaned_html[:250000]}\n```\n\n"
            f"Extraction request: {prompt}\n\n"
            f"Instructions:\n"
            f"Extract the requested data and return ONLY a valid JSON object matching the JSON-LD schema layout below:\n"
            f"{json.dumps(schema)}"
        )
        
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=full_prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )
        
        if response.text:
            parsed = json.loads(response.text.strip())
            print("      🎯 Gemini: Extraction successful.")
            return parsed
        else:
            print("      ⚠️  Gemini: Empty response text.")
            return None
    except Exception as e:
        print(f"      ⚠️  Gemini extraction exception: {e}")
        return None


def extract_product_data(provider: str, client, product: dict) -> dict | None:
    """
    Extracts price and stock data for one product.
    Returns extracted data dict or None on failure.
    """
    name: str = product["name"]
    url:  str = product["url"]
    ptype = product.get("type", "bh_price")

    # ── Simulation mode ────────────────────────────────────────────────────────
    if PRICE_OVERRIDE is not None:
        return {
            "product_title": name,
            "current_price": PRICE_OVERRIDE,
            "stock_status": "Simulated"
        }

    # 1. Try Primary: Firecrawl (if provider is not none)
    if provider != "none":
        res = try_extract_product(provider, client, product)
        if res is not None:
            return res

    # Select prompt and schema based on product type
    if ptype == "iphone_price":
        schema = IPhoneProductSchema.model_json_schema()
        prompt = _IPHONE_PRICE_PROMPT
    else:
        schema = MacBookProductSchema.model_json_schema()
        prompt = _PRICE_PROMPT

    # 2. Try Secondary: ScraperAPI + Gemini
    if SCRAPERAPI_KEY and GEMINI_API_KEY:
        print(f"\n🔄 Primary [{provider.upper()}] failed or bypassed for {name}. Falling back to [SCRAPERAPI + GEMINI]...")
        res = _extract_via_proxy_and_gemini("scraperapi", SCRAPERAPI_KEY, url, schema, prompt)
        if res is not None:
            return res

    # 3. Try Tertiary: Scrape.do + Gemini
    if SCRAPE_DO_KEY and GEMINI_API_KEY:
        print(f"\n🔄 Previous attempts failed for {name}. Falling back to [SCRAPE_DO + GEMINI]...")
        res = _extract_via_proxy_and_gemini("scrapedo", SCRAPE_DO_KEY, url, schema, prompt)
        if res is not None:
            return res

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
    ptype = product.get("type", "bh_price")

    if REPORT_MODE:
        print("  📊 REPORT MODE — sending price report regardless of threshold.")
        notify_price_report(display_title, price, threshold, stock_status, url,
                            product_type=ptype)
    elif price < threshold:
        print("  🚨 BELOW THRESHOLD — sending alert!")
        notify_price_drop(display_title, price, stock_status, url,
                          product_type=ptype)
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
    if not FIRECRAWL_API_KEY and not SCRAPERAPI_KEY:
        print("⚠️  No API keys configured (FIRECRAWL_API_KEY / SCRAPERAPI_KEY).")
        print("   Running in local-only fallback mode (zero-cost direct parsing).")

    if REPORT_MODE:
        print("📊 Running in REPORT MODE — real extraction, always notify with actual price.\n")
    if PRICE_OVERRIDE is not None:
        print(f"⚠️  Running in SIMULATION mode — PRICE_OVERRIDE=${PRICE_OVERRIDE:.2f}\n")

    products = load_active_products()
    if not products:
        print("No active price tracker entries in tracker_config.json. Nothing to check.")
        sys.exit(0)

    print(f"🔍 Checking {len(products)} active product(s).\n")

    provider, client = select_provider()
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
