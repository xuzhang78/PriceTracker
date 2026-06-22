"""
Dealmoon BOGO Sale Event Tracker
---------------------------------
Reads deals_config.json, extracts deals via AI, and sends an ntfy.sh push
notification when a new active "Buy One Get One" (BOGO) deal is detected.

Extraction provider selection (automatic):
  1. Check SGAI credit balance. If credits >= cost of one full run (10 per brand),
     use ScrapeGraphAI (managed browser proxy, high reliability).
  2. If SGAI credits are exhausted or below threshold, automatically fall back
     to Firecrawl (comparable capability, separate free tier).
  Both providers require their own API key set in .env / GitHub Secrets.

Retry logic:
  1. Extract Lancome (up to 3 attempts), then Clinique (up to 3 attempts).
  2. If Lancome failed AND Clinique succeeded → retry Lancome up to 3 more times.
  3. If ALL brands failed → wait FLOW_RETRY_WAIT hours, retry entire flow once.
  4. If retry flow also fails → send error notification and exit 1.

URL logic:
  Config stores baseline URL (expiredClick=n&exp=y = all deals incl. expired).
  Normal daily run swaps BOTH to expiredClick=y&exp=n (active deals only).
  INCLUDE_EXPIRED mode keeps the baseline URL as-is.

State management:
  deals_state.json tracks notified deal titles to prevent duplicate alerts.

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
from dotenv import load_dotenv
from pydantic import BaseModel, Field

# ── Configuration ──────────────────────────────────────────────────────────────

load_dotenv()

SGAI_API_KEY        = os.getenv("SGAI_API_KEY", "")
FIRECRAWL_API_KEY   = os.getenv("FIRECRAWL_API_KEY", "")
NTFY_TOPIC          = os.getenv("NTFY_TOPIC", "")
INCLUDE_EXPIRED     = os.getenv("INCLUDE_EXPIRED", "false").strip().lower() == "true"
TESTING             = os.getenv("TESTING", "false").strip().lower() == "true"
IS_TEST             = INCLUDE_EXPIRED or TESTING

SGAI_CREDITS_PER_CALL = 10      # credits consumed per SGAI extract call
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
        return {"notified_titles": []}


def save_state(state: dict) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def already_notified(state: dict, title: str) -> bool:
    return title in state.get("notified_titles", [])


def mark_notified(state: dict, title: str) -> None:
    state.setdefault("notified_titles", []).append(title)


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


def notify_bogo_deal(brand: str, deal_title: str, description: str, deal_date: str) -> None:
    date_line = f"Posted: {deal_date}\n" if deal_date else ""
    _send_ntfy(
        title=f"🛍️ {brand} BOGO Sale on Dealmoon!",
        body=f"{deal_title}\n{date_line}\n{description}",
        priority="high",
        tags="rotating_light,lipstick,moneybag",
    )
    print("    ✅ BOGO alert sent via ntfy!")


def notify_extraction_error(brands: list[str], provider: str) -> None:
    _send_ntfy(
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


# ── Provider Selection ─────────────────────────────────────────────────────────

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
        # Handle multiple possible response formats across SDK versions
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
    Determines which extraction provider to use for this run.

    Priority:
      1. ScrapeGraphAI — if credit balance >= min_credits_needed
      2. Firecrawl     — automatic fallback when SGAI credits are insufficient

    Returns (provider_name: str, client: object).
    Exits with code 1 if neither provider is available.
    """
    print("🔌 Selecting extraction provider...")

    # ── Check SGAI ──────────────────────────────────────────────────────────────
    remaining = _check_sgai_credits()

    if remaining == -1:
        # Credit check itself failed — attempt SGAI anyway (optimistic)
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

    # ── Fall back to Firecrawl ──────────────────────────────────────────────────
    if FIRECRAWL_API_KEY:
        print("  ✅ Falling back to Firecrawl.")
        from firecrawl import FirecrawlApp
        return "firecrawl", FirecrawlApp(api_key=FIRECRAWL_API_KEY)

    # ── Neither available ───────────────────────────────────────────────────────
    print("❌ No extraction provider available.")
    print("   • SGAI: credits exhausted or key missing → set SGAI_API_KEY")
    print("   • Firecrawl: not configured → set FIRECRAWL_API_KEY")
    sys.exit(1)


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


def _extract_once_sgai(client, url: str, include_expired: bool) -> dict | None:
    """Single ScrapeGraphAI extraction attempt. Returns json_data dict or None."""
    from scrapegraph_py import FetchConfig
    expired_instruction = (
        "Include both active and expired deals." if include_expired
        else "Only include active (non-expired) deals."
    )
    try:
        result = client.extract(
            url=url,
            schema=DealmoonSchema.model_json_schema(),
            prompt=_EXTRACTION_PROMPT.format(expired_instruction=expired_instruction),
            fetch_config=FetchConfig(mode="js", stealth=True, wait=4000),
        )
        if result.status != "success":
            print(f"      ⚠️  SGAI error: {result.error}")
            return None
        data = result.data.json_data
        if not data or not isinstance(data, dict):
            print("      ⚠️  SGAI: json_data empty or malformed.")
            return None
        return data
    except Exception as exc:
        print(f"      ⚠️  SGAI exception: {exc}")
        return None


def _extract_once_firecrawl(client, url: str, include_expired: bool) -> dict | None:
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
        print(f"      ⚠️  Firecrawl exception: {exc}")
        return None


def _extract_once(provider: str, client, url: str, include_expired: bool) -> dict | None:
    """Dispatcher: routes to the correct provider's extraction function."""
    if provider == "sgai":
        return _extract_once_sgai(client, url, include_expired)
    else:
        return _extract_once_firecrawl(client, url, include_expired)


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
        data = _extract_once(provider, client, url, include_expired)
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
    res = try_extract_brand(primary_provider, primary_client, brand_cfg, include_expired, label)
    if res is not None:
        return res

    # Only allow fallback from SGAI to Firecrawl. If primary is Firecrawl, do not fallback to SGAI.
    if primary_provider == "firecrawl":
        return None

    secondary_provider = "firecrawl"
    secondary_key = FIRECRAWL_API_KEY

    if secondary_key:
        print(f"\n🔄 Primary [{primary_provider.upper()}] failed for {brand}. Falling back to [{secondary_provider.upper()}]...")
        try:
            if secondary_provider == "firecrawl":
                from firecrawl import FirecrawlApp
                sec_client = FirecrawlApp(api_key=FIRECRAWL_API_KEY)
            else:
                from scrapegraph_py import ScrapeGraphAI
                sec_client = ScrapeGraphAI(api_key=SGAI_API_KEY)

            res = try_extract_brand(
                secondary_provider, sec_client, brand_cfg, include_expired,
                f"{label} fallback".strip()
            )
            if res is not None:
                return res
        except Exception as exc:
            print(f"  ⚠️  Failed to initialize or extract via fallback provider {secondary_provider.upper()}: {exc}")

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

            if already_notified(state, title):
                print("       ℹ️  Already notified — skipping.")
                continue

            notify_bogo_deal(brand_name, title, description, deal_date)
            mark_notified(state, title)
            state_modified = True

    return state_modified


# ── Entry Point ────────────────────────────────────────────────────────────────

def main() -> None:
    # Require ntfy topic + at least one provider key
    if not NTFY_TOPIC:
        print("❌ Missing NTFY_TOPIC. Set it in .env or GitHub Secrets.")
        sys.exit(1)
    if not SGAI_API_KEY and not FIRECRAWL_API_KEY:
        print("❌ No extraction provider configured.")
        print("   Set SGAI_API_KEY and/or FIRECRAWL_API_KEY in .env or GitHub Secrets.")
        sys.exit(1)

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

    # Select provider based on credit availability
    min_credits = SGAI_CREDITS_PER_CALL * len(active_brands)
    provider, client = select_provider(min_credits)
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
