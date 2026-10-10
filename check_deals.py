"""
Dealmoon BOGO Sale Event Tracker
---------------------------------
Reads tracker_config.json, extracts deals via AI, and sends an ntfy.sh push
notification when a new active "Buy One Get One" (BOGO) deal is detected.

Extraction provider hierarchy:
  1. Primary:   Firecrawl (LLM structured extraction with JS rendering)
  2. Secondary: ScraperAPI proxy + Google Gemini LLM
  3. Tertiary:  Scrape.do proxy + Google Gemini LLM

Retry logic:
  1. Extract each brand (up to 3 attempts), with Lancome-priority retry.
  2. If ALL brands failed → wait FLOW_RETRY_WAIT hours, retry entire flow once.
  3. If retry flow also fails → send error notification and exit 1.

URL logic:
  Config stores baseline URL (expiredClick=n&exp=y = all deals incl. expired).
  Normal daily run swaps BOTH to expiredClick=y&exp=n (active deals only).
  INCLUDE_EXPIRED mode keeps the baseline URL as-is.

State management:
  deals_state.json tracks notified deal IDs and titles to prevent duplicates.

Expired deal detection (3 layers):
  1. Server-side: exp=n in URL so Dealmoon never serves expired content.
  2. AI field: is_expired=True from extracted schema.
  3. Title prefix: title starts with "[Expired]" (Dealmoon server-side marker).

Testing mode (E2E via GitHub Actions only — preserves credits):
  INCLUDE_EXPIRED=true: uses baseline URL (all deals) for pipeline verification.
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
from pydantic import BaseModel, Field
from utils import send_ntfy, select_provider, clean_html, redact

# ── Configuration ──────────────────────────────────────────────────────────────

load_dotenv()

FIRECRAWL_API_KEY   = os.getenv("FIRECRAWL_API_KEY", "")
SCRAPERAPI_KEY      = os.getenv("SCRAPERAPI_KEY", "")
SCRAPE_DO_KEY       = os.getenv("SCRAPE_DO_KEY", "")
GEMINI_API_KEY      = os.getenv("GEMINI_API_KEY", "")
NTFY_TOPIC          = os.getenv("NTFY_TOPIC", "")
INCLUDE_EXPIRED     = os.getenv("INCLUDE_EXPIRED", "false").strip().lower() == "true"
TESTING             = os.getenv("TESTING", "false").strip().lower() == "true"
IS_TEST             = INCLUDE_EXPIRED or TESTING

MAX_RETRIES           = 1 if IS_TEST else 3
RETRY_DELAYS          = [15, 30]
FLOW_RETRY_WAIT       = 2 * 3600

BASE_DIR     = os.path.dirname(__file__)
CONFIG_PATH  = os.path.join(BASE_DIR, "tracker_config.json")   # unified config
STATE_PATH   = os.path.join(BASE_DIR, "deals_state.json")


# ── Pydantic Schema ────────────────────────────────────────────────────────────

class Deal(BaseModel):
    title:       str  = Field(description="The main title or headline of the deal")
    description: str  = Field(
        description="Full details including terms, promo codes, gift items, or expiry"
    )
    deal_date:   str  = Field(
        description=(
            "The date the deal was posted or started on Dealmoon "
            "(e.g. 'May 30', '2026-05-30'). Empty string if not found."
        )
    )
    is_expired:  bool = Field(
        description="True if the deal is explicitly marked as expired on the page"
    )
    deal_url:    str  = Field(
        description="The URL link of the deal page (e.g. 'https://www.dealmoon.com/en/.../5881094.html')"
    )


class DealmoonSchema(BaseModel):
    deals: list[Deal] = Field(description="All deals listed on the page")


# ── State Management ───────────────────────────────────────────────────────────

def load_state() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"notified_titles": [], "notified_ids": []}


def save_state(state: dict) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def _deal_id_from_url(deal_url: str) -> str | None:
    """Extract the numeric deal ID from a Dealmoon URL (e.g. '5881094' from '.../5881094.html')."""
    m = re.search(r'/(\d+)\.html', deal_url)
    return m.group(1) if m else None


def already_notified(state: dict, title: str, deal_url: str = "") -> bool:
    """Check both title-based (legacy) and ID-based dedup."""
    if title in state.get("notified_titles", []):
        return True
    deal_id = _deal_id_from_url(deal_url)
    if deal_id and deal_id in state.get("notified_ids", []):
        return True
    return False


def mark_notified(state: dict, title: str, deal_url: str = "") -> None:
    """Record both title and deal ID for dedup."""
    state.setdefault("notified_titles", []).append(title)
    deal_id = _deal_id_from_url(deal_url)
    if deal_id:
        state.setdefault("notified_ids", []).append(deal_id)


# ── Notification Helpers ───────────────────────────────────────────────────────

def notify_bogo_deal(brand: str, deal_title: str, description: str, deal_date: str) -> None:
    date_line = f"Posted: {deal_date}\n" if deal_date else ""
    send_ntfy(
        title=f"🛍️ {brand} BOGO Sale on Dealmoon!",
        body=f"{deal_title}\n{date_line}\n{description}",
        priority="high",
        tags="rotating_light,lipstick,moneybag",
    )
    print("    ✅ BOGO alert sent via ntfy!")


def notify_extraction_error(brands: list[str], provider: str) -> None:
    send_ntfy(
        title="Deal Tracker — Extraction Failed",
        body=(
            f"All extraction attempts failed for: {', '.join(brands)}\n"
            f"Provider: {provider}\n\n"
            "Both the initial attempts and the 2-hour retry were exhausted."
        ),
        priority="default",
        tags="warning",
    )
    print("  ⚠️  Error alert sent via ntfy!")


# ── Core Extraction ────────────────────────────────────────────────────────────

def _build_url(base_url: str, include_expired: bool) -> str:
    """
    URL parameter semantics (confirmed):
      expiredClick=y & exp=n  →  active deals only   (daily run target)
      expiredClick=n & exp=y  →  all deals incl. expired  (config baseline / E2E)

    Config stores the baseline. Normal daily runs swap both params.
    INCLUDE_EXPIRED mode uses the baseline as-is.
    """
    if include_expired:
        return base_url   # baseline already means all deals
    else:
        url = base_url.replace("expiredClick=n", "expiredClick=y")
        url = url.replace("exp=y", "exp=n")
        return url


_EXTRACTION_PROMPT = (
    "Extract all deals listed on the page. For each deal, capture: "
    "(1) the title/headline, "
    "(2) the full description including terms, promo codes, or gift details, "
    "(3) the date the deal was posted or started — look for timestamps or date "
    "labels near the deal entry (return as a string like 'May 30' or '2026-05-30', "
    "or empty string if not visible), "
    "(4) whether it is marked as expired, "
    "(5) the URL link of the deal page (e.g. 'https://www.dealmoon.com/en/.../5881094.html'). "
    "{expired_instruction} Extract ALL deals listed on the page from top to bottom. "
    "Do not skip or truncate any deals."
)


def parse_dates_from_html(html: str) -> dict[str, str]:
    """
    Parses deal Unix timestamps from raw HTML and maps deal IDs to formatted date strings.
    """
    deal_times = {}
    if not html:
        return deal_times
    deal_blocks = html.split('<div class="mlist')
    for block in deal_blocks[1:]:
        header = block[:1000]
        id_match = re.search(r'data-id="(\d+)"', header)
        t_match = re.search(r'\bt="(\d+)"', header)
        if id_match and t_match:
            deal_id = id_match.group(1)
            timestamp = int(t_match.group(1))
            try:
                dt = datetime.datetime.fromtimestamp(timestamp)
                deal_times[deal_id] = dt.strftime("%b %d, %Y")
            except Exception:
                pass
        else:
            id_match_alt = re.search(r'id="deal(\d+)"', header)
            if id_match_alt and t_match:
                deal_id = id_match_alt.group(1)
                timestamp = int(t_match.group(1))
                try:
                    dt = datetime.datetime.fromtimestamp(timestamp)
                    deal_times[deal_id] = dt.strftime("%b %d, %Y")
                except Exception:
                    pass
    return deal_times


def _extract_once(client, url: str, include_expired: bool) -> dict | None:
    """Single Firecrawl extraction attempt. Returns deals-schema dict or None."""
    expired_instruction = (
        "Include both active and expired deals." if include_expired
        else "Only include active (non-expired) deals."
    )
    try:
        doc = client.scrape(
            url,
            formats=[
                "html",
                {
                    "type": "json",
                    "schema": DealmoonSchema.model_json_schema(),
                    "prompt": _EXTRACTION_PROMPT.format(expired_instruction=expired_instruction)
                }
            ]
        )
        if hasattr(doc, "json") and doc.json is not None:
            if isinstance(doc.json, dict) and "deals" in doc.json:
                result_json = doc.json
                if hasattr(doc, "html") and doc.html:
                    result_json["_html"] = doc.html
                return result_json
        print("      ⚠️  Firecrawl: 'json' field empty or malformed.")
        return None
    except Exception as exc:
        print(f"      ⚠️  Firecrawl exception: {redact(exc)}")
        return None


# ── Brand Extraction with Retry ────────────────────────────────────────────────

def try_extract_brand(
    provider: str,
    client,
    brand_cfg: dict,
    include_expired: bool,
    label: str = "",
) -> list[dict] | None:
    """
    Attempt extraction for one brand up to MAX_RETRIES times.
    Returns list of raw deal dicts, or None if all attempts fail.
    """
    brand = brand_cfg["brand"]
    url   = _build_url(brand_cfg["url"], include_expired)
    tag   = f" [{label}]" if label else ""

    print(f"\n→ {brand}{tag} [{provider.upper()}]")
    print(f"  URL: {url}")

    for attempt in range(1, MAX_RETRIES + 1):
        print(f"  Attempt {attempt}/{MAX_RETRIES}...")
        data = _extract_once(client, url, include_expired)
        if data is not None:
            deals = data.get("deals", [])
            
            # Map Unix timestamps from HTML to the extracted deals via URL deal IDs
            html_content = data.get("_html")
            if html_content:
                deal_times = parse_dates_from_html(html_content)
                mapped_count = 0
                for deal in deals:
                    url_str = deal.get("deal_url", "")
                    id_match = re.search(r'/(\d+)\.html', url_str)
                    if id_match:
                        deal_id = id_match.group(1)
                        if deal_id in deal_times:
                            deal["deal_date"] = deal_times[deal_id]
                            mapped_count += 1
                if mapped_count > 0:
                    print(f"  🎯 Mapped exact dates for {mapped_count}/{len(deals)} deals using HTML timestamps.")
            
            print(f"  ✅ {len(deals)} deal(s) extracted.")
            return deals
        if attempt < MAX_RETRIES:
            delay = RETRY_DELAYS[attempt - 1]
            print(f"  Waiting {delay}s before retry...")
            time.sleep(delay)

    print(f"  ❌ All {MAX_RETRIES} attempts failed for {brand} via {provider.upper()}.")
    return None


def _extract_deals_via_proxy_and_gemini(provider: str, proxy_key: str, url: str, schema: dict, prompt: str) -> list[dict] | None:
    """Fetch HTML using ScraperAPI or Scrape.do proxy, and extract structured JSON using Gemini LLM."""
    if not GEMINI_API_KEY:
        print("      ⚠️  Gemini: GEMINI_API_KEY not configured. Cannot perform LLM extraction.")
        return None

    html_content = ""
    try:
        if provider == "scraperapi":
            print(f"      🔍 ScraperAPI proxy: Fetching HTML for {url}...")
            params = {'api_key': proxy_key, 'url': url}
            r = requests.get('https://api.scraperapi.com/', params=params, timeout=90)
            if r.status_code == 200:
                html_content = r.text
            else:
                print(f"      ⚠️  ScraperAPI: Returned status code {r.status_code}")
                return None
        elif provider == "scrapedo":
            print(f"      🔍 Scrape.do proxy: Fetching HTML for {url}...")
            params = {'token': proxy_key, 'url': url}
            r = requests.get('https://api.scrape.do', params=params, timeout=90)
            if r.status_code == 200:
                html_content = r.text
            else:
                print(f"      ⚠️  Scrape.do: Returned status code {r.status_code}")
                return None
        else:
            return None
    except Exception as e:
        print(f"      ⚠️  Proxy fetch exception via {provider.upper()}: {redact(e)}")
        return None

    if not html_content:
        print(f"      ⚠️  Proxy fetch via {provider.upper()} returned empty content.")
        return None

    # Parse exact date times from HTML for mapping later (if available)
    deal_times = parse_dates_from_html(html_content)

    try:
        print("      🧠 Gemini: Extracting structured data from retrieved HTML...")
        from google.genai import types
        
        # Clean HTML to minimize tokens and prevent truncation
        cleaned_html = clean_html(html_content)
        
        # Initialize unified Client
        client = genai.Client(api_key=GEMINI_API_KEY)
        
        full_prompt = (
            "HTML Content:\n"
            f"```html\n{cleaned_html[:250000]}\n```\n\n"
            f"Extraction request: {prompt}\n\n"
            "Instructions:\n"
            "Extract the requested data and return ONLY a valid JSON object matching the JSON-LD schema layout below:\n"
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
            deals = parsed.get("deals", [])
            
            # Map exact dates via URL deal IDs
            mapped_count = 0
            for deal in deals:
                url_str = deal.get("deal_url", "")
                id_match = re.search(r'/(\d+)\.html', url_str)
                if id_match:
                    deal_id = id_match.group(1)
                    if deal_id in deal_times:
                        deal["deal_date"] = deal_times[deal_id]
                        mapped_count += 1
            if mapped_count > 0:
                print(f"      🎯 Gemini: Mapped exact dates for {mapped_count}/{len(deals)} deals using HTML timestamps.")
            
            print(f"      🎯 Gemini: Extraction successful. {len(deals)} deal(s) parsed.")
            return deals
        else:
            print("      ⚠️  Gemini: Empty response text.")
            return None
    except Exception as e:
        print(f"      ⚠️  Gemini extraction exception: {redact(e)}")
        return None


def try_extract_brand_with_fallback(
    primary_provider: str,
    primary_client,
    brand_cfg: dict,
    include_expired: bool,
    label: str = "",
) -> list[dict] | None:
    """
    Tries to extract brand using primary provider, and dynamically falls back
    to secondary provider (if configured) if the primary fails.
    """
    brand = brand_cfg["brand"]
    url   = _build_url(brand_cfg["url"], include_expired)
    
    # 1. Try Primary: Firecrawl (if provider is not none)
    if primary_provider != "none":
        res = try_extract_brand(primary_provider, primary_client, brand_cfg, include_expired, label)
        if res is not None:
            return res

    schema = DealmoonSchema.model_json_schema()
    expired_instruction = (
        "Important: you must extract ALL deals, including those marked as expired or past."
        if include_expired else
        "Important: focus on active deals only."
    )
    prompt = _EXTRACTION_PROMPT.format(expired_instruction=expired_instruction)

    # 2. Try Secondary: ScraperAPI + Gemini
    if SCRAPERAPI_KEY and GEMINI_API_KEY:
        print(f"\n🔄 Primary [{primary_provider.upper()}] failed or bypassed for {brand}. Falling back to [SCRAPERAPI + GEMINI]...")
        res = _extract_deals_via_proxy_and_gemini("scraperapi", SCRAPERAPI_KEY, url, schema, prompt)
        if res is not None:
            return res

    # 3. Try Tertiary: Scrape.do + Gemini
    if SCRAPE_DO_KEY and GEMINI_API_KEY:
        print(f"\n🔄 Previous attempts failed for {brand}. Falling back to [SCRAPE_DO + GEMINI]...")
        res = _extract_deals_via_proxy_and_gemini("scrapedo", SCRAPE_DO_KEY, url, schema, prompt)
        if res is not None:
            return res

    return None


def count_bogo_matches(deals: list[dict], keywords: list[str]) -> int:
    """Helper to count the number of BOGO matches (active or expired) in a list of deals."""
    if not deals:
        return 0
    count = 0
    for deal in deals:
        title = deal.get("title", "").strip()
        description = deal.get("description", "").strip()
        combined = f"{title} {description}".lower()
        if any(kw in combined for kw in keywords):
            count += 1
    return count


def extract_brand_with_pagination(
    provider: str,
    client,
    brand_cfg: dict,
    include_expired: bool,
    label: str = "",
) -> list[dict] | None:
    """
    Fetch page 1 for a brand, and if running in testing mode and no BOGO match
    is found, dynamically fetch subsequent pages (up to page 3).
    """
    deals = try_extract_brand_with_fallback(provider, client, brand_cfg, include_expired, label)
    if deals is None:
        return None

    if include_expired:
        keywords = [kw.lower() for kw in brand_cfg.get("keywords", [])]
        bogo_count = count_bogo_matches(deals, keywords)
        print(f"  🔍 Page 1: Found {bogo_count} BOGO match(es) for {brand_cfg['brand']}.")
        
        current_page = 1
        max_pages = 3
        while bogo_count == 0 and current_page < max_pages:
            current_page += 1
            print(f"  ⚠️ No BOGO match on page {current_page-1} for {brand_cfg['brand']}. Fetching page {current_page}...")
            
            paginated_cfg = brand_cfg.copy()
            base_url = brand_cfg["url"]
            separator = "&" if "?" in base_url else "?"
            paginated_cfg["url"] = f"{base_url}{separator}p={current_page}"
            
            lbl = f"{label} page {current_page}".strip()
            page_deals = try_extract_brand_with_fallback(
                provider, client, paginated_cfg, include_expired, label=lbl
            )
            if page_deals:
                deals.extend(page_deals)
                new_matches = count_bogo_matches(page_deals, keywords)
                bogo_count += new_matches
                print(f"  🔍 Page {current_page}: Found {new_matches} BOGO match(es) (Total BOGO: {bogo_count}).")
            else:
                print(f"  ⚠️ Failed to extract page {current_page} or page was empty.")
                break
                
    return deals


# ── Orchestration ──────────────────────────────────────────────────────────────

def run_extraction_flow(
    provider: str,
    client,
    active_brands: list[dict],
    include_expired: bool,
) -> dict[str, list[dict] | None]:
    """
    Full extraction flow with Lancome-priority retry:
      1. Extract all brands in config order (Lancome first), each up to 3 attempts.
      2. If Lancome failed AND another brand succeeded → retry Lancome 3 more times.
    Returns brand_name → deals list (or None on failure).
    """
    results: dict[str, list[dict] | None] = {}

    for brand_cfg in active_brands:
        results[brand_cfg["brand"]] = extract_brand_with_pagination(
            provider, client, brand_cfg, include_expired
        )

    # Lancome-priority retry
    lancome_cfg = next((b for b in active_brands if b["brand"] == "Lancome"), None)
    if lancome_cfg and results.get("Lancome") is None:
        other_succeeded = any(v is not None for k, v in results.items() if k != "Lancome")
        if other_succeeded:
            print("\n  ↩️  Lancome failed but other brand(s) succeeded — retrying Lancome...")
            results["Lancome"] = extract_brand_with_pagination(
                provider, client, lancome_cfg, include_expired, label="priority retry"
            )

    return results


# ── Filtering & Notification ───────────────────────────────────────────────────

def process_results(
    results: dict[str, list[dict] | None],
    active_brands: list[dict],
    state: dict,
) -> bool:
    """
    Keyword filter + expired check + dedup → send ntfy alerts.
    Returns True if state was modified.
    """
    state_modified = False
    brand_map = {b["brand"]: b for b in active_brands}

    for brand_name, deals in results.items():
        if deals is None:
            continue

        brand_cfg = brand_map[brand_name]
        keywords  = [kw.lower() for kw in brand_cfg.get("keywords", [])]
        print(f"\n  Processing {brand_name} ({len(deals)} deal(s))...")

        for deal in deals:
            title       = deal.get("title", "").strip()
            description = deal.get("description", "").strip()
            deal_date   = deal.get("deal_date", "").strip()

            # Dual expired detection (AI field + title prefix)
            ai_expired    = deal.get("is_expired", False)
            title_expired = title.lower().startswith("[expired]")
            is_expired    = ai_expired or title_expired

            if not title:
                continue
            if is_expired and not INCLUDE_EXPIRED:
                continue

            combined   = f"{title} {description}".lower()
            matched_kw = next((kw for kw in keywords if kw in combined), None)
            if not matched_kw:
                continue

            print(f"\n    🎯 BOGO match! Keyword: '{matched_kw}'")
            print(f"       Title:  {title}")
            print(f"       Date:   {deal_date or 'N/A'}")
            print(f"       Status: {'EXPIRED' if is_expired else 'ACTIVE'}")

            deal_url = deal.get("deal_url", "")
            if already_notified(state, title, deal_url):
                print("       ℹ️  Already notified — skipping.")
                continue

            notify_bogo_deal(brand_name, title, description, deal_date)
            mark_notified(state, title, deal_url)
            state_modified = True

    return state_modified


# ── Entry Point ────────────────────────────────────────────────────────────────

def main() -> None:
    # Require ntfy topic + at least one provider key
    if not NTFY_TOPIC:
        print("❌ Missing NTFY_TOPIC. Set it in .env or GitHub Secrets.")
        sys.exit(1)
    if not FIRECRAWL_API_KEY and not SCRAPERAPI_KEY:
        print("⚠️  No API keys configured (FIRECRAWL_API_KEY / SCRAPERAPI_KEY).")

    if INCLUDE_EXPIRED:
        print("⚠️  Running in INCLUDE_EXPIRED mode — expired deals processed for E2E testing.\n")

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)

    # Filter only dealmoon_bogo entries from the unified tracker_config.json
    all_brands    = [t for t in config.get("trackers", []) if t.get("type") == "dealmoon_bogo"]
    active_brands = [b for b in all_brands if b.get("active", True)]
    if not active_brands:
        print("No active dealmoon_bogo entries in tracker_config.json. Nothing to check.")
        sys.exit(0)

    brand_names = [b["brand"] for b in active_brands]
    print(f"🔍 Checking {len(active_brands)} brand(s): {', '.join(brand_names)}\n")

    provider, client = select_provider()
    print()

    state = load_state()

    # ── First extraction flow ──────────────────────────────────────────────────
    print("── Extraction flow (attempt 1) ──")
    results = run_extraction_flow(provider, client, active_brands, INCLUDE_EXPIRED)

    # ── Full-flow retry on total failure ───────────────────────────────────────
    if all(v is None for v in results.values()):
        if IS_TEST:
            print("\n⚠️ All brands failed, but running in testing mode — skipping 2-hour retry sleep.")
            notify_extraction_error(brand_names, provider)
            sys.exit(1)

        wait_hours = FLOW_RETRY_WAIT // 3600
        print(f"\n⏳ All brands failed. Retrying entire flow in {wait_hours}h...")
        time.sleep(FLOW_RETRY_WAIT)

        print("\n── Extraction flow (attempt 2 — after retry wait) ──")
        results = run_extraction_flow(provider, client, active_brands, INCLUDE_EXPIRED)

        if all(v is None for v in results.values()):
            print("\n❌ Retry flow also failed. Sending error notification.")
            notify_extraction_error(brand_names, provider)
            sys.exit(1)

    # ── Filter + notify ────────────────────────────────────────────────────────
    state_modified = process_results(results, active_brands, state)

    if state_modified:
        save_state(state)
        print("\n💾 State saved to deals_state.json.")

    print("\n✅ All checks complete.")


if __name__ == "__main__":
    main()
