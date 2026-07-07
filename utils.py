"""
Shared utilities for PriceTracker scripts.

Provides common functions used by both check_prices.py and check_deals.py:
  - clean_html:       Strip CSS/SVG/comment/script bloat from HTML before LLM processing
  - send_ntfy:        POST push notifications to ntfy.sh
  - select_provider:  Choose the primary extraction provider (Firecrawl or none)
"""

import os
import re
import time

import requests


# ── Configuration (loaded once at import time) ────────────────────────────────

from dotenv import load_dotenv
load_dotenv()

FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY", "")
NTFY_TOPIC        = os.getenv("NTFY_TOPIC", "")


# ── HTML Minimization ─────────────────────────────────────────────────────────

def clean_html(html: str, *, keep_catalog_scripts: bool = False) -> str:
    """Remove bloat from HTML (styles, SVGs, comments, scripts) to save LLM tokens.

    Args:
        html: Raw HTML string.
        keep_catalog_scripts: If True, preserve <script> tags containing
            product catalog markers (e.g. SKU data, prices).  Used by
            check_prices.py for Verizon variant extraction.  If False,
            all scripts are stripped (suitable for Dealmoon pages).
    """
    if not html:
        return ""

    # 1. Remove <style> blocks
    html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)

    # 2. Remove <svg> elements
    html = re.sub(r'<svg[^>]*>.*?</svg>', '', html, flags=re.DOTALL | re.IGNORECASE)

    # 3. Remove HTML comments
    html = re.sub(r'<!---->|<!--.*?-->', '', html, flags=re.DOTALL)

    # 4. Handle <script> tags
    if keep_catalog_scripts:
        _CATALOG_MARKERS = [
            "sku6037331", "currentPrice", "buyProductMaxPurch",
            "regularPrice", "productTitle", "addButtonState",
        ]
        def _script_filter(match):
            content = match.group(0)
            return content if any(m in content for m in _CATALOG_MARKERS) else ""
        html = re.sub(r'<script[^>]*>.*?</script>', _script_filter,
                       html, flags=re.DOTALL | re.IGNORECASE)
    else:
        html = re.sub(r'<script[^>]*>.*?</script>', '', html,
                       flags=re.DOTALL | re.IGNORECASE)

    # 5. Compress consecutive blank lines
    html = re.sub(r'\n\s*\n', '\n', html)

    return html.strip()


# ── Push Notifications ────────────────────────────────────────────────────────

def send_ntfy(title: str, body: str, *, priority: str = "default",
              tags: str = "", topic: str = "") -> None:
    """POST a push notification to ntfy.sh with ASCII-safe header, up to 3 attempts."""
    topic = topic or NTFY_TOPIC
    safe_title = title.encode("ascii", errors="replace").decode("ascii")
    for attempt in range(1, 4):
        try:
            response = requests.post(
                f"https://ntfy.sh/{topic}",
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
    print(f"    ❌ ntfy: all 3 attempts failed — alert '{safe_title}' was NOT delivered.")


# ── Provider Selection ────────────────────────────────────────────────────────

def select_provider() -> tuple[str, object]:
    """Select Firecrawl as the primary extraction provider, or fall back to proxies."""
    print("🔌 Selecting extraction provider...")

    if FIRECRAWL_API_KEY:
        print("  ✅ Firecrawl configured as primary provider.")
        from firecrawl import FirecrawlApp
        return "firecrawl", FirecrawlApp(api_key=FIRECRAWL_API_KEY)

    print("  ℹ️ Firecrawl is not configured. Relying on backup proxies (ScraperAPI / Scrape.do) + Gemini.")
    return "none", None
