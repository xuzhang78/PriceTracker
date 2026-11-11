"""
Promo Code / Fee-Waiver Tracker
-------------------------------
Reads tracker_config.json and watches pages for seasonal promotions, e.g. the
American Express gift card "100% off purchase and shipping fees" offer that
usually runs in mid-December with a code such as EPIC<YY>.

Each `promo_watch` entry lists regex patterns. `{YY}` and `{YYYY}` are replaced
with the current year, so "EPIC{YY}" matches EPIC26 in 2026 and EPIC27 in 2027.

  - code_patterns:   searched in the raw HTML and the visible text
  - phrase_patterns: searched in the visible text only; any one match alerts
  - combo_patterns:  named groups of patterns; alerts only when EVERY group
                     matches within combo_window characters of each other
                     (e.g. a purchase-fee waiver AND a shipping-fee waiver)

No LLM is involved. Detection is plain pattern matching on a rendered page.

Fetch hierarchy (first usable page wins):
  1. Firecrawl rendered scrape (1 credit, cache disabled)
  2. ScraperAPI proxy with JS rendering
  3. Scrape.do proxy with JS rendering
  4. Direct HTTP fetch (0 credits; often blocked or unrendered)

Run modes:
  Normal run:      python check_promos.py
  Simulate a hit:  PROMO_OVERRIDE="Use code EPIC26 for 100% off purchase fees" python check_promos.py
                   → skips fetching and state, sends a [TEST] notification on match
"""

import datetime
import html as html_lib
import json
import os
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

import requests
from dotenv import load_dotenv
from utils import send_ntfy, select_provider, clean_html, redact

# ── Configuration ──────────────────────────────────────────────────────────────

load_dotenv()

SCRAPERAPI_KEY  = os.getenv("SCRAPERAPI_KEY", "")
SCRAPE_DO_KEY   = os.getenv("SCRAPE_DO_KEY", "")
NTFY_TOPIC      = os.getenv("NTFY_TOPIC", "")
PROMO_OVERRIDE  = os.getenv("PROMO_OVERRIDE", "").strip()
TESTING         = os.getenv("TESTING", "false").strip().lower() == "true"
IS_TEST         = bool(PROMO_OVERRIDE) or TESTING

BASE_DIR     = os.path.dirname(__file__)
CONFIG_PATH  = os.path.join(BASE_DIR, "tracker_config.json")
STATE_PATH   = os.path.join(BASE_DIR, "promo_state.json")

# A rendered page with less visible text than this is treated as blocked/empty
MIN_PAGE_TEXT_CHARS = 300
SNIPPET_RADIUS      = 80
DEFAULT_COMBO_WINDOW = 250
MAX_MATCHES_IN_ALERT = 5

_BLOCK_MARKERS = re.compile(
    r"access denied|unusual traffic|verify you are (?:a )?human|captcha|"
    r"pardon our interruption|request blocked|enable javascript",
    re.IGNORECASE,
)
_DIAGNOSTIC_TERMS = re.compile(r"promo(?:tion)? code|purchase fee|shipping fee|% off", re.IGNORECASE)


# ── State Management ───────────────────────────────────────────────────────────

def load_state() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
            if isinstance(state, dict) and isinstance(state.get("notified"), list):
                return state
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {"notified": []}


def save_state(state: dict) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


# ── Matching ───────────────────────────────────────────────────────────────────

def expand_pattern(pattern: str, today: datetime.date) -> str:
    """Substitute the {YY} / {YYYY} year placeholders in a config pattern."""
    return pattern.replace("{YYYY}", f"{today.year:04d}").replace("{YY}", f"{today.year % 100:02d}")


def visible_text(html: str) -> str:
    """Reduce HTML to whitespace-normalised visible text."""
    if not html:
        return ""
    text = clean_html(html)                      # drops style, svg, comments, scripts
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _snippet(text: str, start: int, end: int) -> str:
    lo, hi = max(0, start - SNIPPET_RADIUS), min(len(text), end + SNIPPET_RADIUS)
    snippet = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text[lo:hi])).strip()
    return ("…" if lo > 0 else "") + snippet + ("…" if hi < len(text) else "")


def find_matches(entry: dict, raw: str, text: str, today: datetime.date) -> list[dict]:
    """Return one dict per distinct match: {kind, match, snippet}.

    code_patterns are searched in the visible text first, then the raw HTML
    (promo codes can sit in embedded JSON). phrase_patterns are searched in
    the visible text only, to avoid matching script internals.
    combo_patterns yield a single match, and only when every group matches
    close together in the visible text.
    """
    found: dict[str, dict] = {}

    def search(patterns: list[str], kind: str, haystacks: list[str]) -> None:
        for pattern in patterns:
            expanded = expand_pattern(pattern, today)
            try:
                regex = re.compile(expanded, re.IGNORECASE)
            except re.error as exc:
                print(f"    ⚠️  Invalid {kind} pattern {pattern!r}: {exc}")
                continue
            for haystack in haystacks:
                for m in regex.finditer(haystack):
                    key = re.sub(r"\s+", " ", m.group(0)).strip().lower()
                    if key and key not in found:
                        found[key] = {
                            "kind": kind,
                            "match": re.sub(r"\s+", " ", m.group(0)).strip(),
                            "snippet": _snippet(haystack, m.start(), m.end()),
                        }

    search(entry.get("code_patterns", []), "code", [text, raw])
    search(entry.get("phrase_patterns", []), "phrase", [text])

    combo = find_combo(entry, text, today)
    if combo:
        found.setdefault(combo["match"].lower(), combo)
    return list(found.values())


def find_combo(entry: dict, text: str, today: datetime.date) -> dict | None:
    """Match combo_patterns: every named group must hit within combo_window chars.

    Returns the tightest cluster as one match, or None. A page that waives
    only the purchase fee, or only shipping, therefore produces nothing.
    """
    groups = entry.get("combo_patterns") or {}
    if not groups:
        return None
    window = int(entry.get("combo_window", DEFAULT_COMBO_WINDOW))

    spans: dict[str, list[tuple[int, int]]] = {}
    for label, patterns in groups.items():
        hits = []
        for pattern in patterns:
            try:
                regex = re.compile(expand_pattern(pattern, today), re.IGNORECASE)
            except re.error as exc:
                print(f"    ⚠️  Invalid combo pattern {pattern!r}: {exc}")
                continue
            hits.extend(m.span() for m in regex.finditer(text))
        if not hits:
            return None                          # one group missing → no combo
        spans[label] = hits

    best: tuple[int, int, int] | None = None     # (extent, start, end)
    first, *rest = spans.values()
    for a_start, a_end in first:
        lo, hi = a_start, a_end
        for hits in rest:
            # nearest hit of this group to the anchor
            b_start, b_end = min(hits, key=lambda h: max(h[0] - a_end, a_start - h[1], 0))
            if max(b_start - a_end, a_start - b_end, 0) > window:
                break
            lo, hi = min(lo, b_start), max(hi, b_end)
        else:
            if best is None or hi - lo < best[0]:
                best = (hi - lo, lo, hi)
    if best is None:
        return None

    _, lo, hi = best
    return {
        "kind": "combo",
        "match": " + ".join(spans),              # e.g. "purchase fee waived + shipping fee waived"
        "snippet": _snippet(text, lo, hi),
    }


def in_window(entry: dict, today: datetime.date) -> bool:
    """True when today falls inside the entry's optional start_date / end_date window."""
    start, end = entry.get("start_date"), entry.get("end_date")
    if start and today < datetime.date.fromisoformat(start):
        return False
    if end and today > datetime.date.fromisoformat(end):
        return False
    return True


# ── Fetching ───────────────────────────────────────────────────────────────────

def _usable(text: str) -> bool:
    """Reject empty shells and bot-block interstitials."""
    if len(text) < MIN_PAGE_TEXT_CHARS:
        return False
    if len(text) < 3000 and _BLOCK_MARKERS.search(text):
        return False
    return True


def _fetch_firecrawl(client, url: str) -> tuple[str, str] | None:
    print("    🔍 Firecrawl: rendered scrape (1 credit, cache disabled)...")
    doc = client.scrape(
        url,
        formats=["markdown", "rawHtml"],
        only_main_content=False,   # promo banners usually live in the header
        wait_for=5000,             # let client-side banners render
        max_age=0,                 # never serve a cached copy
        timeout=60000,
    )
    raw      = getattr(doc, "raw_html", None) or ""
    markdown = getattr(doc, "markdown", None) or ""
    return raw, f"{markdown} {visible_text(raw)}".strip()


def _fetch_proxy(provider: str, key: str, url: str) -> tuple[str, str] | None:
    if provider == "scraperapi":
        print("    🔍 ScraperAPI proxy: rendered fetch...")
        r = requests.get("https://api.scraperapi.com/",
                         params={"api_key": key, "url": url, "render": "true"}, timeout=90)
    else:
        print("    🔍 Scrape.do proxy: rendered fetch...")
        r = requests.get("https://api.scrape.do",
                         params={"token": key, "url": url, "render": "true"}, timeout=90)
    if r.status_code != 200:
        print(f"    ⚠️  {provider}: returned status code {r.status_code}")
        return None
    return r.text, visible_text(r.text)


def _fetch_direct(url: str) -> tuple[str, str] | None:
    print("    🔍 Direct HTTP fetch (0 credits)...")
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    r = requests.get(url, headers=headers, timeout=20)
    if r.status_code != 200:
        print(f"    ⚠️  Direct fetch: returned status code {r.status_code}")
        return None
    return r.text, visible_text(r.text)


def fetch_page(provider: str, client, url: str) -> tuple[str, str, str] | None:
    """Fetch a page through the provider chain.

    Returns (raw_html, visible_text, source) from the first provider that
    yields a usable page, or None if every provider fails or is blocked.
    """
    attempts = []
    if provider != "none" and client is not None:
        attempts.append(("firecrawl", lambda: _fetch_firecrawl(client, url)))
    if SCRAPERAPI_KEY:
        attempts.append(("scraperapi", lambda: _fetch_proxy("scraperapi", SCRAPERAPI_KEY, url)))
    if SCRAPE_DO_KEY:
        attempts.append(("scrapedo", lambda: _fetch_proxy("scrapedo", SCRAPE_DO_KEY, url)))
    attempts.append(("direct", lambda: _fetch_direct(url)))

    for source, fetch in attempts:
        try:
            result = fetch()
        except Exception as exc:
            print(f"    ⚠️  {source} exception: {redact(exc)}")
            continue
        if result is None:
            continue
        raw, text = result
        if not _usable(text):
            print(f"    ⚠️  {source}: page looks empty or blocked ({len(text)} chars of text).")
            continue
        print(f"    ✅ {source}: {len(text)} chars of visible text.")
        return raw, text, source
    return None


def print_diagnostics(text: str) -> None:
    """Log a few promo-related snippets so a run log shows what the page said."""
    seen, shown = set(), 0
    for m in _DIAGNOSTIC_TERMS.finditer(text):
        snippet = _snippet(text, m.start(), m.end())
        if snippet in seen:
            continue
        seen.add(snippet)
        print(f"       · {snippet}")
        shown += 1
        if shown >= 6:
            break
    if not shown:
        print("       · (no promo-related wording on the page)")


# ── Notification Helpers ───────────────────────────────────────────────────────

def notify_promo(entry: dict, matches: list[dict]) -> None:
    lines = [f"{m['match']}: {m['snippet'][:200]}" for m in matches[:MAX_MATCHES_IN_ALERT]]
    if len(matches) > MAX_MATCHES_IN_ALERT:
        lines.append(f"(+{len(matches) - MAX_MATCHES_IN_ALERT} more)")
    prefix = "[TEST] " if PROMO_OVERRIDE else ""
    send_ntfy(
        title=f"{prefix}Promo detected: {entry['name']}",
        body="\n".join(lines) + f"\n\n{entry['url']}",
        priority="high",
        tags="rotating_light,credit_card,moneybag",
    )
    print("    ✅ Promo alert sent via ntfy!")


def notify_fetch_error(names: list[str]) -> None:
    send_ntfy(
        title="Promo Tracker — Fetch Failed",
        body="Could not load a usable page for:\n" + "\n".join(names)
             + "\n\nEvery provider failed or was blocked. Promo detection is blind until this is fixed.",
        priority="default",
        tags="warning",
    )
    print("  ⚠️  Error alert sent via ntfy!")


# ── Entry Point ────────────────────────────────────────────────────────────────

def check_entry(entry: dict, provider: str, client, state: dict, today: datetime.date) -> bool:
    """Check one promo_watch entry. Returns False only if the page could not be fetched."""
    name, url = entry["name"], entry["url"]
    print(f"\n→ {name}")
    print(f"  URL: {url}")

    if PROMO_OVERRIDE:
        raw, text, source = PROMO_OVERRIDE, visible_text(PROMO_OVERRIDE), "override"
    else:
        page = fetch_page(provider, client, url)
        if page is None:
            print(f"  ❌ All providers failed for {name}.")
            return False
        raw, text, source = page
        print("    Promo-related wording seen:")
        print_diagnostics(text)

    matches = find_matches(entry, raw, text, today)
    if not matches:
        print(f"  ✅ No promo detected (source: {source}).")
        return True

    for m in matches:
        print(f"  🎯 {m['kind']} match: {m['match']!r}  ←  {m['snippet']}")

    if PROMO_OVERRIDE:
        notify_promo(entry, matches)             # simulation: no dedup, no state
        return True

    fresh = [m for m in matches
             if f"{name}|{today.year}|{m['match'].lower()}" not in state["notified"]]
    if not fresh:
        print("  ℹ️  Already notified — skipping.")
        return True

    notify_promo(entry, fresh)
    state["notified"].extend(f"{name}|{today.year}|{m['match'].lower()}" for m in fresh)
    return True


def main() -> None:
    if not NTFY_TOPIC:
        print("❌ Missing NTFY_TOPIC. Set it in .env or GitHub Secrets.")
        sys.exit(1)

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)

    today   = datetime.date.today()
    entries = []
    for entry in config.get("trackers", []):
        if entry.get("type") != "promo_watch" or not entry.get("active", False):
            continue
        if not in_window(entry, today):
            if IS_TEST:
                print(f"  🧪 Testing mode: bypassing date window for '{entry['name']}'")
            else:
                print(f"  ⏳ Skipping '{entry['name']}' — outside "
                      f"{entry.get('start_date', '…')} to {entry.get('end_date', '…')}")
                continue
        entries.append(entry)

    if not entries:
        print("No active promo_watch entries in tracker_config.json. Nothing to check.")
        sys.exit(0)

    print(f"🔍 Checking {len(entries)} promo page(s).")

    if PROMO_OVERRIDE:
        print("⚠️  Running in SIMULATION mode — PROMO_OVERRIDE text replaces the page. No fetch, no state.\n")
        provider, client = "none", None
    else:
        provider, client = select_provider()

    state   = load_state()
    before  = len(state["notified"])
    failed  = [e["name"] for e in entries
               if not check_entry(e, provider, client, state, today)]

    if not PROMO_OVERRIDE:
        save_state(state)
        if len(state["notified"]) != before:
            print("\n💾 State saved to promo_state.json.")

    if failed:
        notify_fetch_error(failed)
        sys.exit(1)

    print("\n✅ All checks complete.")


if __name__ == "__main__":
    main()
