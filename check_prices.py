"""
B&H Photo MacBook Pro M5 Price Tracker
---------------------------------------
Reads products.json, uses ScrapeGraphAI to AI-extract prices,
and sends an ntfy.sh push notification when price drops below threshold.

Usage:
  Normal run:       python check_prices.py
  Simulate alert:   PRICE_OVERRIDE=2499.00 python check_prices.py
"""

import json
import os
import sys

# Force UTF-8 output so emoji print correctly on Windows terminals
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

import requests
from dotenv import load_dotenv
from pydantic import BaseModel
from scrapegraph_py import FetchConfig, ScrapeGraphAI

# ── Configuration ──────────────────────────────────────────────────────────────

load_dotenv()

SGAI_API_KEY = os.getenv("SGAI_API_KEY", "")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")
_price_override_raw = os.getenv("PRICE_OVERRIDE", "").strip()
PRICE_OVERRIDE: float | None = float(_price_override_raw) if _price_override_raw else None

# Prices outside this range indicate a failed / blocked extraction
MIN_VALID_PRICE = 100.0
MAX_VALID_PRICE = 10_000.0


# ── Data Schema ────────────────────────────────────────────────────────────────

class MacBookProductSchema(BaseModel):
    """Structured fields extracted by ScrapeGraphAI from each product page."""
    product_title: str
    current_price: float   # USD, numeric only — no $ or commas
    stock_status: str


# ── Notification Helpers ───────────────────────────────────────────────────────

def _send_ntfy(title: str, body: str, priority: str = "default", tags: str = "") -> None:
    """POST a push notification to ntfy.sh."""
    try:
        response = requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=body.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": priority,
                "Tags": tags,
            },
            timeout=10,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        print(f"    ⚠️  ntfy send failed: {exc}")


def notify_price_drop(product_title: str, price: float, stock_status: str, url: str) -> None:
    """Send a high-priority price-drop alert."""
    _send_ntfy(
        title="B&H MacBook Price Drop!",
        body=f"Price dropped to ${price:.2f}\n{product_title}\nStock: {stock_status}\n{url}",
        priority="high",
        tags="rotating_light,apple,moneybag",  # ntfy renders these as emoji in the app
    )
    print("    ✅ Price drop alert sent via ntfy!")


def notify_extraction_error(product_name: str, reason: str) -> None:
    """Send a warning alert when extraction fails or sanity check fails."""
    _send_ntfy(
        title="Price Tracker - Extraction Error",
        body=f"Failed to extract price for:\n{product_name}\n\nReason: {reason}",
        priority="default",
        tags="warning",
    )
    print("    ⚠️  Error alert sent via ntfy!")


# ── Telegram Alternative ───────────────────────────────────────────────────────
# To switch from ntfy to Telegram:
#   1. Add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to your .env / GitHub Secrets
#   2. Replace calls to notify_price_drop() and notify_extraction_error() with
#      the send_telegram() function below.
#
# def send_telegram(message: str) -> None:
#     token = os.getenv("TELEGRAM_BOT_TOKEN", "")
#     chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
#     requests.get(
#         f"https://api.telegram.org/bot{token}/sendMessage",
#         params={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
#         timeout=10,
#     )


# ── Core Logic ─────────────────────────────────────────────────────────────────

def load_active_products() -> list[dict]:
    """Load products.json and return only entries where active == true."""
    products_path = os.path.join(os.path.dirname(__file__), "products.json")
    with open(products_path, "r", encoding="utf-8") as f:
        all_products = json.load(f)
    return [p for p in all_products if p.get("active", True)]


def check_product(sgai: ScrapeGraphAI, product: dict) -> None:
    """Extract price for one product and trigger notification if below threshold."""
    name: str = product["name"]
    url: str = product["url"]
    threshold: float = float(product["threshold"])

    print(f"→ {name}")
    print(f"  URL: {url}")
    print(f"  Threshold: ${threshold:.2f}")

    # ── Simulation mode ────────────────────────────────────────────────────────
    if PRICE_OVERRIDE is not None:
        print(f"  ℹ️  PRICE_OVERRIDE active — using ${PRICE_OVERRIDE:.2f} (real price not fetched)")
        price = PRICE_OVERRIDE
        product_title = name
        stock_status = "Simulated"

    # ── Live extraction mode ───────────────────────────────────────────────────
    else:
        try:
            result = sgai.extract(
                url=url,
                schema=MacBookProductSchema.model_json_schema(),  # SDK requires a dict, not a class
                prompt=(
                    "Extract the product title, the current retail price in USD as a plain "
                    "float (no currency symbols, no commas), and the stock availability status."
                ),
                fetch_config=FetchConfig(mode="js", stealth=True, wait=2000),
            )
        except Exception as exc:
            print(f"  ❌ Unexpected SDK error: {exc}")
            notify_extraction_error(name, str(exc))
            return

        if result.status == "error":
            print(f"  ❌ Extraction failed: {result.error}")
            notify_extraction_error(name, result.error or "Unknown error")
            return

        # result.data is a plain dict when schema is passed as a JSON schema dict
        try:
            parsed = MacBookProductSchema(**result.data)
        except Exception as exc:
            print(f"  ❌ Failed to parse extraction result: {exc}")
            notify_extraction_error(name, f"Parse error: {exc}")
            return

        price = parsed.current_price
        product_title = parsed.product_title
        stock_status = parsed.stock_status

        print(f"  Title:  {product_title}")
        print(f"  Stock:  {stock_status}")

    print(f"  Price:  ${price:.2f}")

    # ── Sanity guard ───────────────────────────────────────────────────────────
    # A price outside the valid range almost certainly means the AI extracted
    # text from a Cloudflare challenge page instead of the real product page.
    if not (MIN_VALID_PRICE < price < MAX_VALID_PRICE):
        reason = (
            f"Extracted price ${price:.2f} is outside the valid range "
            f"(${MIN_VALID_PRICE:.0f}–${MAX_VALID_PRICE:,.0f}). "
            "Possible Cloudflare block or page layout change."
        )
        print(f"  ❌ Sanity check failed — {reason}")
        notify_extraction_error(name, reason)
        return

    # ── Price threshold check ──────────────────────────────────────────────────
    if price < threshold:
        print(f"  🚨 BELOW THRESHOLD — sending alert!")
        display_title = product_title if PRICE_OVERRIDE is None else name
        notify_price_drop(display_title, price, stock_status, url)
    else:
        gap = price - threshold
        print(f"  ✅ Above threshold by ${gap:.2f} — no alert.")


# ── Entry Point ────────────────────────────────────────────────────────────────

def main() -> None:
    # Validate required env vars
    missing = [k for k, v in {"SGAI_API_KEY": SGAI_API_KEY, "NTFY_TOPIC": NTFY_TOPIC}.items() if not v]
    if missing:
        print(f"❌ Missing required environment variable(s): {', '.join(missing)}")
        print("   Set them in your .env file (local) or GitHub Secrets (CI).")
        sys.exit(1)

    if PRICE_OVERRIDE is not None:
        print(f"⚠️  Running in SIMULATION mode — PRICE_OVERRIDE=${PRICE_OVERRIDE:.2f}\n")

    products = load_active_products()
    if not products:
        print("No active products in products.json. Nothing to check.")
        sys.exit(0)

    print(f"🔍 Checking {len(products)} active product(s)...\n")

    sgai = ScrapeGraphAI(api_key=SGAI_API_KEY)

    for product in products:
        check_product(sgai, product)
        print()

    print("✅ All checks complete.")


if __name__ == "__main__":
    main()
