#!/usr/bin/env python3
"""
Pokemon TCG MSRP Watcher (Singapore) - v3
-------------------------------------------
Watches Singapore listings of sealed Pokemon TCG product and alerts on
Telegram when one is worth buying to RESELL:

  - English and Japanese product -> priced against TCGplayer's market price
    (via tcgcsv.com, refreshed daily) for that exact sealed product,
    converted to SGD. "Deal" = at or below 75% of market and at least S$20
    below it; "Check" = under 50% (usually a different product, a variant's
    price or a scam, but worth a look).
  - Simplified Chinese product -> priced against a hand-kept list of local
    resale prices in chinese_prices.json.
  - The official Pokemon Store on Lazada -> alerts when an item is in stock
    (checked on its product page) at a retail price well below market, i.e.
    a hot restock, and again whenever it restocks.

Carousell is checked from Kenny's Mac (WATCHER_MODE=local, see mac/), which
also sends one alert per newly posted buyout lot.

No cart automation, no stored credentials beyond the Telegram bot token -
every alert is just a link to the listing; checkout is always manual.

Credentials: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID from env vars, or a
gitignored secrets.json next to this script (see load_telegram_credentials()).

Usage:
    python3 pokemon_msrp_watcher.py
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
# Where this copy runs. "cloud" = the GitHub Action (every site except
# Carousell); "local" = Kenny's Mac (Carousell only), because Carousell
# blocks cloud servers but not a home connection (confirmed Nov 2026).
MODE = os.environ.get("WATCHER_MODE", "cloud").strip().lower()
STATE_FILE = ROOT / ("state_local.json" if MODE == "local" else "state.json")
CATALOG_CACHE_FILE = ROOT / "catalog_cache.json"
SECRETS_FILE = ROOT / "secrets.json"  # gitignored
CHINESE_PRICES_FILE = ROOT / "chinese_prices.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

CATALOG_REFRESH_HOURS = 4
DIGEST_TOP_N = 5


@dataclass
class Target:
    name: str
    url: str
    parser: str
    runs_on: str = "cloud"  # "cloud" (GitHub Action) or "local" (Mac)


def load_telegram_credentials() -> tuple[str, str]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if (not token or not chat_id) and SECRETS_FILE.exists():
        secrets = json.loads(SECRETS_FILE.read_text())
        token = token or secrets.get("telegram_bot_token", "")
        chat_id = chat_id or secrets.get("telegram_chat_id", "")
    return token, chat_id


TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID = load_telegram_credentials()


# ======================================================================
# Category classification (first-pass filter, PRD §7.4)
# ======================================================================
CATEGORIES = [
    # (label, keywords, most-specific-first)
    ("Elite Trainer Box", ["elite trainer box", "etb"]),
    ("Booster bundle", ["booster bundle"]),
    # "booster display box" / "boosters display box" catches real listings
    # like "...Chaos Rising Booster Display Box" and "...Perfect Order
    # Boosters Display Box" (plural on the FIRST word) - caught live during
    # testing, where these were falling through to the generic "booster"
    # fallback and getting misclassified as a single pack instead of a box.
    ("Booster box", ["booster box", "booster display box", "boosters display box"]),
    ("Premium/Special collection", ["premium collection", "special collection", "collection box"]),
    ("Tin", ["tin"]),
    ("Single booster pack", ["booster pack", "booster"]),
]


def _keyword_matches(lowered_title: str, keyword: str) -> bool:
    """Whole-word/phrase match with optional trailing s/es for plurals.
    Bugs caught in a prior build: raw substring match on "tin" false-positived
    inside "destiny"; singular-only keywords missed "6 Booster Boxes"."""
    pattern = r"\b" + re.escape(keyword) + r"(e?s)?\b"
    return re.search(pattern, lowered_title) is not None


def classify(title: str) -> str | None:
    lowered = title.lower()
    if "pokemon" not in lowered and "pokémon" not in lowered:
        return None
    for label, keywords in CATEGORIES:
        if any(_keyword_matches(lowered, kw) for kw in keywords):
            return label
    return None


# ======================================================================
# Language detection (PRD §7.3)
# ======================================================================
CHINESE_MARKERS = [
    "chinese", " cn ", "(cn)", "[cn]", " chn ", "[chi]", "(chi)", "simplified", "s-chinese",
    "中文", "简体", "簡體",
]


def detect_language(title: str) -> str:
    lowered = title.lower()
    # Chinese first: these titles often also say e.g. "Japanese art", but a
    # Chinese marker means the product itself is the Chinese print.
    if any(m in f" {lowered} " for m in CHINESE_MARKERS):
        return "chinese"
    japanese_markers = ["japanese", " jap ", " jp ", "(jp)", "jap.", " jpn"]
    if any(m in f" {lowered} " for m in japanese_markers) or lowered.startswith("jap"):
        return "japanese"
    return "english"


# ======================================================================
# Fuzzy matching against a reference catalog (PRD §7.5)
# ======================================================================
# Common words/phrases that show up on BOTH sides (catalog entries and
# retailer listings) but don't help identify which SET a product is from -
# stripped before comparing so what's left is mostly just the set name.
# Caught live: Lazada catalog entries themselves carry noise like
# "[Limit 2 per person]" and "Pokémon Center" branding that a retailer's
# own listing of the same set often doesn't repeat verbatim.
_BOILERPLATE_PATTERNS = [
    r"\[.*?\]",  # bracketed suffixes like "[Limit 2 per person]"
    r"\(.*?\)",  # parenthetical suffixes like "(m2)", "(sv11b)"
    r"\blimit\b.*",  # anything from "limit" onward (limit N per person)
    r"\bpokemon\b", r"\bpokémon\b",
    r"\btrading card game\b", r"\btcg\b",
    r"\bcent(er|re)\b", r"\bpc\b", r"\bpkc\b", r"\boriginal\b",
    r"\bstandard edition\b",
    r"\bjapanese\b", r"\bjap\b", r"\bjp\b",
    r"\bchinese\b", r"\bsimplified\b", r"\bcn\b",
    r"\bshrinked?\b", r"\bunshrink(ed)?\b", r"\bcase\b", r"\bsealed\b",
]


def _normalize_for_match(text: str, category: str | None = None) -> str:
    text = text.lower()
    for pattern in _BOILERPLATE_PATTERNS:
        text = re.sub(pattern, " ", text)
    if category:
        for label, keywords in CATEGORIES:
            if label == category:
                for kw in keywords:
                    text = re.sub(r"\b" + re.escape(kw) + r"(e?s)?\b", " ", text)
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def find_catalog_match(title: str, category: str, catalog: list[dict], threshold: float = 0.8) -> dict | None:
    """category must match exactly; within that, fuzzy-match the (short)
    catalog set name against the (long, noisy) listing title.

    NOT a plain SequenceMatcher.ratio() on the two full strings - caught
    live during testing: that scores a clean match like catalog entry
    "abyss eye" against listing "pokemon tcg abyss eye japanese booster
    box shrinked" as only ~0.30 similarity, because ratio() penalizes the
    listing's extra boilerplate words as if they were a mismatch, even
    though the catalog title is a perfect substring. Instead: exact
    substring containment = 1.0 confidence; otherwise, the fraction of the
    catalog entry's own words that appear anywhere in the listing title
    (word-set recall) - short catalog names mean this stays a meaningful
    signal even with a high threshold.

    Threshold is an implementation detail to tune against real data
    (PRD §7.5, §11.5)."""
    # Pokemon Center and "Plus" editions are different products from the
    # standard ones (caught live Nov 2026: a S$89.90 Standard Perfect Order
    # ETB matched the S$109.90 Pokemon Center one and alerted as -18%).
    flags = _edition_flags(title)
    candidates = [c for c in catalog if c["category"] == category and _edition_flags(c["title"]) == flags]
    if not candidates:
        return None
    norm_title = _normalize_for_match(title, category)
    title_words = set(norm_title.split())

    best = None
    best_score = 0.0
    for entry in candidates:
        entry_norm = _normalize_for_match(entry["title"], category)
        entry_words = set(entry_norm.split())
        if not entry_words:
            continue
        if entry_norm in norm_title:
            score = 1.0
        else:
            score = len(entry_words & title_words) / len(entry_words)
        if score > best_score:
            best_score = score
            best = entry
    if best and best_score >= threshold:
        return best
    return None


# ======================================================================
# English/International reference catalog (Lazada) - PRD §7.1
# ======================================================================
# NOTE: the store's generic "All-Products" view (q=All-Products, no
# sub-category) is dominated by merchandise (plush, plates, tote bags) across
# 34 pages - confirmed live that sealed TCG product doesn't appear on page 1
# under default sort. The store's "Lots & Card Game Packs" sub-category is
# where actual sealed product (ETBs, boosters, tins, UPCs) lives - confirmed
# live, only 2 pages.
LAZADA_CATALOG_URL_TEMPLATE = (
    "https://www.lazada.sg/shop-lots-&-card-game-packs/"
    "?pokemon-store-online-singapore&from=wangpu&m=shop&q=All-Products&page={page}"
)
LAZADA_CATALOG_MAX_PAGES = 5  # safety cap in case the category grows


def fetch_rendered_html(url: str, wait_ms: int = 4000) -> str:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(user_agent=HEADERS["User-Agent"])
            page.goto(url, timeout=30000, wait_until="load")
            page.wait_for_timeout(wait_ms)
            return page.content()
        finally:
            browser.close()


def parse_price(text: str) -> float | None:
    match = re.search(r"[\d,]+\.\d{2}|\d+", text.replace(",", ""))
    return float(match.group()) if match else None


def _fetch_lazada_raw_listings(wait_ms: int = 4000) -> list[dict]:
    """Scrape the Lazada Pokemon Store Online Singapore storefront's "Lots &
    Card Game Packs" sub-category (sealed product) into raw listing dicts
    {title, price, url, in_stock}. Selectors verified against live HTML -
    Lazada is a React SPA with hashed CSS-module class names that WILL
    rotate on redeploy; re-verify if this starts returning zero entries.
    Paginated (currently 2 pages) - stops early once a page returns no tiles.
    Shared by both fetch_english_catalog() (the reference-price source) and
    check_lazada() (Lazada as an ordinary checked retailer, PRD §6)."""
    listings = []
    seen_titles = set()
    for page_num in range(1, LAZADA_CATALOG_MAX_PAGES + 1):
        page_url = LAZADA_CATALOG_URL_TEMPLATE.format(page=page_num)
        html_content = fetch_rendered_html(page_url, wait_ms=wait_ms)
        soup = BeautifulSoup(html_content, "html.parser")
        tiles = soup.select(".Bm3ON > div")
        if not tiles:
            break

        for tile in tiles:
            pdp_links = tile.select('a[href*="/products/pdp-"]')
            if not pdp_links:
                continue
            title_link = next((a for a in pdp_links if a.get_text(strip=True)), None)
            if title_link:
                title = title_link.get_text(strip=True)
            else:
                img = tile.select_one("img[alt]")
                title = img["alt"] if img else None
            if not title or title in seen_titles:
                continue
            seen_titles.add(title)

            price_el = tile.select_one(".ooOxS")
            price = parse_price(price_el.get_text()) if price_el else None
            if price is None:
                continue

            href = pdp_links[0].get("href")
            url = urljoin(page_url, href) if href else None

            # No confirmed live sold-out example was found during research -
            # falls back to a text scan as a best-effort guess.
            text_blob = tile.get_text(" ", strip=True).lower()
            in_stock = not any(oos in text_blob for oos in ("sold out", "out of stock"))

            listings.append({"title": title, "price": price, "url": url, "in_stock": in_stock})
    return listings


def lazada_product_in_stock(url: str) -> bool:
    """The store's product grid shows sold-out items exactly like available
    ones (confirmed 5 Nov 2026: every item checked said "Out of stock" only on
    its own product page). In stock = a buy button and no "out of stock".
    Anything unclear counts as out of stock, so no alert is sent."""
    try:
        text = BeautifulSoup(fetch_rendered_html(url, wait_ms=3000), "html.parser").get_text(" ").lower()
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] stock check failed for {url}: {exc}", file=sys.stderr)
        return False
    has_buy_button = "add to cart" in text or "buy now" in text
    return has_buy_button and "out of stock" not in text


def fetch_english_catalog() -> list[dict]:
    """{title, category, price} reference catalog, built from the raw Lazada
    listings (PRD §7.1)."""
    catalog = []
    for listing in _fetch_lazada_raw_listings():
        category = classify(listing["title"])
        if category is None:
            continue
        catalog.append({"title": listing["title"], "category": category, "price": listing["price"]})
    return catalog


def check_lazada(target: "Target") -> list[dict]:
    """Lazada checked as an ordinary retailer too (PRD §6) - its current
    live price is compared against the CACHED reference catalog (which may
    be up to CATALOG_REFRESH_HOURS old), so a temporary flash-sale discount
    below its own regular price is still catchable."""
    # The store page intermittently renders with no products (2 of 4 runs
    # on 5 Nov 2026). Retry with longer waits before giving up, and fail
    # loudly so an empty result shows as an error in the run log.
    for attempt, wait_ms in enumerate((4000, 8000, 12000), start=1):
        listings = _fetch_lazada_raw_listings(wait_ms=wait_ms)
        if listings:
            if attempt > 1:
                print(f"[lazada] loaded on attempt {attempt}")
            return [{**listing, "target_name": target.name} for listing in listings if listing["in_stock"]]
    raise RuntimeError("store page showed no products after 3 attempts")


# ======================================================================
# Japanese reference catalog (pokemon-card.com) - PRD §7.2
# ======================================================================
JP_PRODUCTS_URL = (
    "https://www.pokemon-card.com/products/index.html"
    "?productType=expansion&dateLowerY=2024&dateLowerM=1&dateLowerD=1"
    "&dateUpperY=2027&dateUpperM=12&dateUpperD=31"
)
JP_PACKS_PER_BOX = 30

# Katakana set name -> English-romanized set name. Small, manageable,
# hand-maintained list (PRD §7.2, §11.7) - expand as new sets release.
# Seeded from sets cross-referenced live between pokemon-card.com and
# DXCOLLECTION/Carousell listings during PRD research.
JP_SET_NAME_MAP = {
    "ストームエメラルダ": "Storm Emeralda",
    "アビスアイ": "Abyss Eye",
    "ニンジャスピナー": "Ninja Spinner",
    "ムニキスゼロ": "Nihil Muniki Zero",
    "MEGAドリームex": "Mega Dream ex",
    "インフェルノX": "Inferno X",
    "メガブレイブ": "Mega Brave",
    "メガシンフォニア": "Mega Symphonia",
    "ブラックボルト": "Black Bolt",
    "ホワイトフレア": "White Flare",
    "ロケット団の栄光": "Glory of Team Rocket",
    "熱風のアリーナ": "Scorching Arena",
    "バトルパートナーズ": "Battle Partners",
    "30th CELEBRATION": "30th Anniversary",
}


def fetch_jp_pack_prices() -> list[dict]:
    """Scrape pokemon-card.com's official product list -> [{title_jp, price_jpy}]
    (per-pack price, tax included). JS-rendered - confirmed via curl returning
    zero .product-card matches during PRD research."""
    html_content = fetch_rendered_html(JP_PRODUCTS_URL)
    soup = BeautifulSoup(html_content, "html.parser")

    results = []
    for card in soup.select(".product-card"):
        title_el = card.select_one(".product-title span")
        if not title_el:
            continue
        title_jp = title_el.get_text(strip=True)

        price_jpy = None
        for table in card.select(".product-table"):
            spans = table.select("span")
            if len(spans) >= 2 and "希望小売価格" in spans[0].get_text():
                price_jpy = parse_price(spans[1].get_text())
                break
        if price_jpy is None:
            continue

        results.append({"title_jp": title_jp, "price_jpy_per_pack": price_jpy})
    return results


def fetch_jpy_sgd_rate() -> float:
    """Free, no-API-key exchange rate source (PRD §7.2)."""
    resp = requests.get(
        "https://api.frankfurter.dev/v1/latest", params={"base": "JPY", "symbols": "SGD"}, timeout=15
    )
    resp.raise_for_status()
    return resp.json()["rates"]["SGD"]


def build_japanese_catalog() -> list[dict]:
    """Combine pack prices (JPY) -> box reference prices (SGD), matched to
    English set names via JP_SET_NAME_MAP. Only "Booster box" category is
    derivable this way for now (30 packs/box assumption, PRD §11.6) -
    other Japanese categories aren't covered by this source yet."""
    pack_prices = fetch_jp_pack_prices()
    jpy_sgd_rate = fetch_jpy_sgd_rate()

    catalog = []
    for entry in pack_prices:
        # "拡張パックデラックス" (Deluxe Expansion Pack) is a genuinely
        # different, pricier product (caught live: ~5x the regular pack
        # price for the same set name, e.g. Black Bolt ¥290 regular vs
        # ¥1450 deluxe) - skip it rather than let it silently overwrite/
        # collide with the regular box price under the same set name.
        if "デラックス" in entry["title_jp"]:
            continue

        set_name_jp = re.sub(r"^拡張パック", "", entry["title_jp"])
        set_name_jp = re.sub(r"^ハイクラスパック", "", set_name_jp)
        set_name_jp = set_name_jp.strip("「」 ")
        english_name = JP_SET_NAME_MAP.get(set_name_jp)
        if not english_name:
            continue  # not in our mapping yet - skip rather than guess

        box_price_jpy = entry["price_jpy_per_pack"] * JP_PACKS_PER_BOX
        box_price_sgd = round(box_price_jpy * jpy_sgd_rate, 2)
        catalog.append({"title": english_name, "category": "Booster box", "price": box_price_sgd})
    return catalog


# ======================================================================
# Catalog caching (PRD §7.1/§7.2 - refreshed every N hours, not every poll)
# ======================================================================
def _load_catalog_cache() -> dict:
    if CATALOG_CACHE_FILE.exists():
        return json.loads(CATALOG_CACHE_FILE.read_text())
    return {}


def _save_catalog_cache(cache: dict) -> None:
    CATALOG_CACHE_FILE.write_text(json.dumps(cache, indent=2))


def load_chinese_prices() -> tuple[list[dict], float]:
    """Hand-kept resale reference list for Chinese product. Read fresh on
    every run (no network, nothing to cache). Returns (catalog, deal_pct).
    A missing or broken file just means no Chinese matches this run."""
    try:
        data = json.loads(CHINESE_PRICES_FILE.read_text())
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] could not read {CHINESE_PRICES_FILE.name}: {exc}", file=sys.stderr)
        return [], 80.0
    items = [
        {"title": i["title"], "category": i["category"], "price": float(i["price"])}
        for i in data.get("items", [])
        if i.get("title") and i.get("category") and i.get("price")
    ]
    return items, float(data.get("deal_pct", 80))


def get_catalog(language: str) -> list[dict]:
    """Returns the cached catalog for "english" or "japanese", refreshing it
    if stale (older than CATALOG_REFRESH_HOURS) or missing. On a refresh
    failure, falls back to the last successfully cached version (PRD §8.12
    resilience) rather than blocking the whole run."""
    import time

    cache = _load_catalog_cache()
    entry = cache.get(language)
    now = time.time()

    if MODE == "local":
        # The Mac copy only reads the catalogs the GitHub run keeps fresh and
        # commits (pulled before each local run). Never scrape or write the
        # cache locally: that would leave the Mac's git checkout dirty and
        # break the next pull.
        return entry["catalog"] if entry else []
    is_stale = entry is None or (now - entry.get("fetched_at", 0)) > CATALOG_REFRESH_HOURS * 3600

    if is_stale:
        try:
            fresh = fetch_english_catalog() if language == "english" else build_japanese_catalog()
            if not fresh:
                # An empty scrape is a failure (blocked page, changed markup),
                # not "the store sells nothing". Caught live Nov 2026: Lazada
                # returned nothing from a GitHub runner and the good cached
                # English catalog was overwritten with an empty list.
                raise RuntimeError("scrape returned 0 entries")
            cache[language] = {"fetched_at": now, "catalog": fresh}
            _save_catalog_cache(cache)
            return fresh
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] failed to refresh {language} catalog: {exc}", file=sys.stderr)
            if entry:
                print(f"[warn] falling back to cached {language} catalog", file=sys.stderr)
                return entry["catalog"]
            return []
    return entry["catalog"]


# ======================================================================
# Resale market prices (TCGplayer, via tcgcsv.com) - added Nov 2026
# ----------------------------------------------------------------------
# Kenny buys to resell, so the useful question for any listing is "how far
# below what it resells for is this?", not "is it at official retail?".
# TCGplayer's market price (USD) for every English and Japanese sealed
# product is a good resale yardstick: checked 5 Nov 2026 against Carousell
# asks (e.g. Japanese 30th Celebration box US$204 = ~S$261 vs S$190-280
# asks). Covers older/out-of-print product the official store no longer
# sells, which is where most resale margin is.
# ======================================================================
TCGCSV = "https://tcgcsv.com/tcgplayer"
MARKET_CATEGORY_IDS = {"english": 3, "japanese": 85}
MARKET_REFRESH_HOURS = 24
DEAL_PCT_OF_MARKET = 75  # alert at or below this % of market
MIN_GAP_SGD = 20  # ...and at least this far below it, so cheap packs don't alert
CHECK_BELOW_PCT_OF_MARKET = 50  # below this, likely a mismatch, a variant's price or a scam
FALLBACK_USD_SGD = 1.29

# TCGplayer products that aren't a single sealed item a seller would list
# on its own (cases, displays of bundles/tins, code cards, sets of N...).
_NOT_SINGLE_SEALED = re.compile(
    r"\b(code card|case|cases|set of|bundle of|lot|bulk|sleeves?|deck box|binder|playmat|"
    r"portfolio|pins?|coins?|figure|plush|jumbo|oversized?|poster|stamp|accessor(y|ies))\b"
    r"|\b(bundle|tin|pack|booster) display\b",
    re.I,
)

_PC_RE = re.compile(r"pok[eé]mon cent(er|re)|\bpc\b|\bpkc\b", re.I)
_PLUS_RE = re.compile(r"\bplus\b", re.I)
_MINI_RE = re.compile(r"\bmini\b", re.I)


def _edition_flags(title: str) -> tuple[bool, bool, bool]:
    """(Pokemon Center edition, Plus edition, Mini). Caught live: a S$39.90
    "30th Celebration ex Tin" matched the "30th Celebration Mini Tin"."""
    return bool(_PC_RE.search(title)), bool(_PLUS_RE.search(title)), bool(_MINI_RE.search(title))


def sealed_products_from_group(products: list[dict], prices: list[dict], code: str = "") -> list[dict]:
    """Pure: one tcgcsv group's products + prices -> sealed catalog entries
    {title, category, usd}. Singles carry a "Number" field; sealed don't."""
    best_price: dict[int, float] = {}
    for pr in prices:
        mp = pr.get("marketPrice")
        if mp:
            best_price[pr["productId"]] = max(best_price.get(pr["productId"], 0), float(mp))
    out = []
    for prod in products:
        if any(e.get("name") == "Number" for e in prod.get("extendedData") or []):
            continue
        name = prod.get("name") or ""
        usd = best_price.get(prod.get("productId"))
        if not usd or _NOT_SINGLE_SEALED.search(name):
            continue
        category = classify("pokemon " + name)
        if category is None:
            continue
        entry = {"title": name, "category": category, "usd": round(usd, 2)}
        if code:
            entry["code"] = code.lower()
        out.append(entry)
    return out


def _tcgcsv_results(url: str, tries: int = 4) -> list[dict]:
    """tcgcsv rate-limits bursts with 401/429 (caught on the first live run:
    77 Japanese sets refused at 8 parallel requests). Back off and retry."""
    import time

    for attempt in range(tries):
        resp = requests.get(url, headers=HEADERS, timeout=30)
        if resp.status_code in (401, 403, 429) or resp.status_code >= 500:
            if attempt < tries - 1:
                time.sleep(2 * (attempt + 1) ** 2)
                continue
        resp.raise_for_status()
        return resp.json().get("results", [])
    return []


def fetch_market_catalog(language: str) -> list[dict]:
    from concurrent.futures import ThreadPoolExecutor

    cat = MARKET_CATEGORY_IDS[language]
    groups = _tcgcsv_results(f"{TCGCSV}/{cat}/groups")

    def one(group: dict) -> list[dict]:
        gid = group["groupId"]
        try:
            return sealed_products_from_group(
                _tcgcsv_results(f"{TCGCSV}/{cat}/{gid}/products"),
                _tcgcsv_results(f"{TCGCSV}/{cat}/{gid}/prices"),
                group.get("abbreviation") or "",
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] market prices for group {gid} failed: {exc}", file=sys.stderr)
            return []

    with ThreadPoolExecutor(max_workers=3) as pool:
        entries = [e for chunk in pool.map(one, groups) for e in chunk]
    if len(entries) < 100:
        raise RuntimeError(f"only {len(entries)} sealed products found")
    return entries


def fetch_usd_sgd_rate() -> float:
    resp = requests.get(
        "https://api.frankfurter.dev/v1/latest", params={"base": "USD", "symbols": "SGD"}, timeout=15
    )
    resp.raise_for_status()
    return float(resp.json()["rates"]["SGD"])


def _cached(key: str, fetch, refresh_hours: float, default):
    """Generic version of get_catalog's cache: refresh when stale, fall back
    to the last good value on failure, never refresh on the Mac."""
    import time

    cache = _load_catalog_cache()
    entry = cache.get(key)
    now = time.time()
    if MODE == "local":
        return entry["value"] if entry else default
    if entry and (now - entry.get("fetched_at", 0)) <= refresh_hours * 3600:
        return entry["value"]
    try:
        value = fetch()
        cache = _load_catalog_cache()  # re-read: another catalog may have been saved meanwhile
        cache[key] = {"fetched_at": now, "value": value}
        _save_catalog_cache(cache)
        return value
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] failed to refresh {key}: {exc}", file=sys.stderr)
        return entry["value"] if entry else default


def get_market_catalogs() -> tuple[dict[str, list[dict]], float]:
    market = {
        lang: _cached(f"market_{lang}", lambda lang=lang: fetch_market_catalog(lang), MARKET_REFRESH_HOURS, [])
        for lang in MARKET_CATEGORY_IDS
    }
    rate = _cached("usd_sgd", fetch_usd_sgd_rate, 24, FALLBACK_USD_SGD)
    return market, float(rate)


# Words that say nothing about WHICH product a listing is.
_LISTING_NOISE = {
    "instock", "in", "stock", "ready", "new", "brand", "english", "eng", "en", "free",
    "delivery", "shrink", "po", "preorder", "authentic", "official", "wts", "wtt", "sale",
    "cheap", "cheapest", "bb", "card", "cards", "game", "the", "and", "of", "with", "for",
    "pokemon", "pokémon", "box", "boxe", "condition", "mint", "set", "sg", "only",
    "sleeved", "loose", "random", "series", "unshrink", "available", "now", "selling",
    "shopbased", "shop", "based", "carousell", "preferred", "merchant", "great",
}
# An entry whose name is ONLY these words can't identify a product
# (caught live: "Super-Premium Collection [Mew & Mewtwo]" reduced to "super"
# and matched every "Super Premium Collection" listing at S$6,000).
_GENERIC_ENTRY_WORDS = {"super", "ultra", "premium", "special", "deluxe", "mini", "collection",
                        "sleeved", "booster", "pack", "box", "bundle", "tin"}
_EDITION_NUMBER = re.compile(r"^(\d+(st|nd|rd|th)|151)$")
_ALIASES = [(re.compile(r"\banniversary\b", re.I), "celebration"),
            (re.compile(r"\bfestival\b", re.I), "fest")]


def _market_words(text: str, category: str, keep_brackets: bool = False) -> list[str]:
    """Normalised words for market matching. Variant names TCGplayer puts in
    brackets ("[Koraidon]", "[Day]") are kept for catalog entries; plurals,
    "ex" and common naming variants (Anniversary/Celebration,
    Festival/Fest) are folded so sellers' wording still matches."""
    if keep_brackets:
        text = re.sub(r"[\[\]()]", " ", text)
    for pattern, repl in _ALIASES:
        text = pattern.sub(repl, text)
    words = []
    for w in _normalize_for_match(text, category).split():
        if w == "ex":
            continue
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        words.append(w)
    return words


def _prepared_entry(entry: dict) -> dict:
    """Cache each catalog entry's normalised words (computed once per run)."""
    if "_words" not in entry:
        words = _market_words(entry["title"], entry["category"], keep_brackets=True)
        entry["_words"] = words
        entry["_wordset"] = set(words)
        entry["_numbers"] = {w for w in words if _EDITION_NUMBER.match(w)}
        entry["_generic"] = all(w in _GENERIC_ENTRY_WORDS for w in words)
        entry["_flags"] = _edition_flags(entry["title"])
    return entry


def find_market_match(title: str, category: str, catalog: list[dict]) -> dict | None:
    """Match a listing to a TCGplayer sealed product. Beyond the basic
    word-overlap match:
      - Pokemon Center / Plus editions must agree;
      - edition numbers (30th, 25th, 151) must agree;
      - a set code in the title (M6a, SV8a...) narrows to that set;
      - among full matches, the most specific entry wins;
      - short titles ("151 ETB") match the entry containing all their words
        with the fewest extras."""
    flags = _edition_flags(title)
    words = _market_words(title, category)
    title_words = set(words)
    numbers = {w for w in title_words if _EDITION_NUMBER.match(w)}
    norm_title = " ".join(words)

    candidates = []
    for entry in catalog:
        if entry["category"] != category:
            continue
        e = _prepared_entry(entry)
        if e["_flags"] != flags or e["_generic"] or not e["_words"]:
            continue
        candidates.append(e)
    # A set code in the title (M6a, SV8a) is the strongest signal there is:
    # trust it over the edition-number check.
    codes = {e.get("code") for e in candidates if e.get("code") and e["code"] in title_words}
    if codes:
        candidates = [e for e in candidates if e.get("code") in codes]
    else:
        candidates = [e for e in candidates if e["_numbers"] == numbers]
    if not candidates:
        return None

    best, best_key = None, (0.0, 0)
    for e in candidates:
        if f" {' '.join(e['_words'])} " in f" {norm_title} ":
            score = 1.0
        else:
            score = len(e["_wordset"] & title_words) / len(e["_wordset"])
        key = (score, len(e["_wordset"]))
        if score >= 0.8 and key > best_key:
            best, best_key = e, key
    if best:
        return best

    core = {w for w in title_words if w not in _LISTING_NOISE and w not in codes}
    if 1 <= len(core) <= 3 and not core <= _GENERIC_ENTRY_WORDS:
        fits = [e for e in candidates if core <= e["_wordset"]]
        if fits:
            return min(fits, key=lambda e: len(e["_wordset"]))
    return None


# Listings that aren't one sealed product as sold: graded cards, empty or
# opened boxes, accessories, wanted ads, code cards.
_NOT_SEALED_LISTING = re.compile(
    r"\b(psa|bgs|cgc|graded|slab|no packs?|empty|box only|opened|acrylic|protector|"
    r"magnetic|accessor(y|ies)|code cards?|codes|wtb|looking for)\b",
    re.I,
)


def evaluate_listing(raw: dict, market: dict[str, list[dict]], chinese: list[dict],
                     usd_sgd: float, chinese_deal_pct: float = 80.0) -> dict | None:
    """Price one listing against resale value. Returns None when it isn't a
    sealed product we can identify; otherwise the item with
    verdict "deal" | "check" | "none"."""
    if not raw.get("in_stock", True):
        return None
    title = raw["title"]
    category = classify(title)
    if category is None or _NOT_SEALED_LISTING.search(title):
        return None
    # Two or more set names = a dealer's list or keyword stuffing, not one
    # product (caught live: "pitch perfect phantasmal flames 30th booster box").
    if len({m.lower() for m in _SET_NAMES.findall(title)}) >= 2:
        return None
    language = detect_language(title)

    if language == "chinese":
        match = find_catalog_match(title, category, chinese)
        if not match:
            return None
        ref_sgd, source, deal_pct = float(match["price"]), "local resale list", chinese_deal_pct
    else:
        match = find_market_match(title, category, market.get(language, []))
        if not match:
            return None
        ref_sgd = round(match["usd"] * usd_sgd, 2)
        source, deal_pct = f"TCGplayer US${match['usd']:.2f}", DEAL_PCT_OF_MARKET

    price = float(raw["price"])
    pct = price / ref_sgd * 100 if ref_sgd else 999
    if pct < CHECK_BELOW_PCT_OF_MARKET:
        verdict = "check"
    elif pct <= deal_pct and ref_sgd - price >= MIN_GAP_SGD:
        verdict = "deal"
    else:
        verdict = "none"
    return {
        "title": title,
        "category": category,
        "language": language,
        "price": price,
        "market_sgd": ref_sgd,
        "market_title": match["title"],
        "source": source,
        "pct_of_market": round(pct),
        "verdict": verdict,
        "url": _clean_url(raw["url"]),
        "retailer": raw["target_name"],
    }


# ======================================================================
# Retailer parsers (PRD §6, §8.3) - each returns raw listing dicts
# {title, price, url, in_stock, target_name}. Classification, language
# detection, catalog matching, and price comparison happen centrally in
# process_listing(), not per-parser.
# ======================================================================

# ---- Toys"R"Us (Salesforce Commerce Cloud) -----------------------------
# Selectors verified against live HTML in a prior build:
#   tile: .product-tile | title/link: .card-link | price: .price .value[content]
#   stock: in-stock tiles have NO .status element; unavailable tiles have
#   <div class="status">unavailable</div> (also "coming soon" for pre-orders).
def check_toysrus(target: Target) -> list[dict]:
    resp = requests.get(target.url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    listings = []
    for tile in soup.select(".product-tile"):
        status_el = tile.select_one(".status")
        in_stock = not (status_el and status_el.get_text(strip=True).lower() in ("unavailable", "coming soon"))

        link_el = tile.select_one(".card-link")
        if not link_el:
            continue
        title = link_el.get_text(strip=True)
        href = link_el.get("href")
        if not href:
            continue
        url = urljoin(target.url, href)

        price_el = tile.select_one(".price .value")
        price = None
        if price_el and price_el.get("content"):
            try:
                price = float(price_el["content"])
            except ValueError:
                price = parse_price(price_el.get_text())
        elif price_el:
            price = parse_price(price_el.get_text())
        if price is None:
            continue

        listings.append({"title": title, "price": price, "url": url, "in_stock": in_stock, "target_name": target.name})
    return listings


# ---- WooCommerce (DXCOLLECTION, and reusable for other local shops) ----
# tile: .product | title: .wd-entities-title a (falls back to common
# WooCommerce title classes) | price: .price .woocommerce-Price-amount
# (prefers "ins" sale price over "del" struck-through original)
# stock: WooCommerce's own instock/outofstock class on the product wrapper.
def _extract_woocommerce_style(target: Target, soup: BeautifulSoup) -> list[dict]:
    listings = []
    for item in soup.select(".product"):
        classes = item.get("class") or []
        in_stock = "outofstock" not in classes

        title_el = (
            item.select_one(".wd-entities-title a")
            or item.select_one(".woocommerce-loop-product__title")
            or item.select_one(".product-title, h2.product-name, h3.product-name")
        )
        link_el = title_el if (title_el and title_el.name == "a") else item.select_one("a")
        if not title_el or not link_el:
            continue
        title = title_el.get_text(strip=True)
        href = link_el.get("href")
        if not href:
            continue
        url = urljoin(target.url, href)

        price_el = item.select_one(".price")
        if not price_el:
            continue
        amount_el = (
            price_el.select_one("ins .woocommerce-Price-amount, ins .amount")
            or price_el.select_one(".woocommerce-Price-amount, .amount")
        )
        price = parse_price(amount_el.get_text() if amount_el else price_el.get_text())
        if price is None:
            continue

        listings.append({"title": title, "price": price, "url": url, "in_stock": in_stock, "target_name": target.name})
    return listings


def check_dxcollection(target: Target) -> list[dict]:
    resp = requests.get(target.url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    return _extract_woocommerce_style(target, soup)


def check_woocommerce_generic(target: Target) -> list[dict]:
    """Reusable for any other WooCommerce-based local card shop - point a
    new Target at this. Relies on the standard instock/outofstock class
    and common title-class fallbacks, so it should work without per-site
    tuning for most WooCommerce themes."""
    resp = requests.get(target.url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    return _extract_woocommerce_style(target, soup)


# ---- Amazon.sg (best-effort, unverified per PRD §6) --------------------
def check_amazon(target: Target) -> list[dict]:
    resp = requests.get(target.url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    listings = []
    for item in soup.select("[data-component-type='s-search-result']"):
        title_el = item.select_one("h2 a span")
        price_el = item.select_one(".a-price .a-offscreen")
        link_el = item.select_one("h2 a")
        if not title_el or not price_el or not link_el:
            continue
        title = title_el.get_text(strip=True)
        price = parse_price(price_el.get_text())
        href = link_el.get("href")
        if price is None or not href:
            continue
        url = urljoin(target.url, href)
        listings.append({"title": title, "price": price, "url": url, "in_stock": True, "target_name": target.name})
    return listings


# ---- Carousell (JS-rendered, noisiest source per PRD §6) ---------------
# tile: a[href^="/p/"] (the listing link doubles as the tile)
# title: img[alt] within the link (avoids parsing the concatenated
# "Buyer Protection...Title...S$X" text blob) | price: regex "S$<amount>"
def check_carousell(target: Target) -> list[dict]:
    html_content = fetch_rendered_html(target.url)
    soup = BeautifulSoup(html_content, "html.parser")

    listings = []
    seen_hrefs = set()
    for link in soup.select('a[href^="/p/"]'):
        href = link.get("href")
        if not href or href in seen_hrefs:
            continue
        seen_hrefs.add(href)

        text_blob = link.get_text(" ", strip=True).lower()
        in_stock = not any(m in text_blob for m in ("sold", "reserved"))

        img = link.select_one("img[alt]")
        title = img["alt"] if img else None
        if not title:
            continue

        price_match = re.search(r"S\$\s?([\d,]+(?:\.\d{2})?)", link.get_text(" ", strip=True))
        price = float(price_match.group(1).replace(",", "")) if price_match else None
        if price is None:
            continue

        url = urljoin(target.url, href)
        listings.append({"title": title, "price": price, "url": url, "in_stock": in_stock, "target_name": target.name})
    return listings


PARSERS = {
    "toysrus": check_toysrus,
    "dxcollection": check_dxcollection,
    "woocommerce_generic": check_woocommerce_generic,
    "amazon": check_amazon,
    "carousell": check_carousell,
    "lazada": check_lazada,
}


# ======================================================================
# Target configuration (PRD §6) - unified polling, no tiering.
# ======================================================================
TARGETS = [
    Target(
        name="Pokemon Store Online Singapore (Lazada, official)",
        url="https://www.lazada.sg/shop-lots-&-card-game-packs/?pokemon-store-online-singapore&from=wangpu&m=shop&q=All-Products",
        parser="lazada",
    ),
    # Toys"R"Us and Amazon.sg removed Nov 2026: Toys"R"Us SG sells no
    # Pokemon cards online (its /pokemon/ page is toys and figures), and
    # Amazon returned 0 listings on every run. Parsers kept for later.
    Target(
        name="DXCOLLECTION - Booster Boxes",
        url="https://dxcollection.com.sg/product-category/pokemon-cards-singapore/pokemon-booster-boxes/",
        parser="dxcollection",
    ),
    Target(
        name="DXCOLLECTION - Elite Trainer Boxes",
        url="https://dxcollection.com.sg/product-category/pokemon-cards-singapore/pokemon-elite-trainer-boxes-etb/",
        parser="dxcollection",
    ),
    Target(
        name="Carousell - Pokemon booster box search",
        url="https://www.carousell.sg/search/pokemon%20booster%20box?sort_by=3",
        parser="carousell",
        runs_on="local",
    ),
    Target(
        name="Carousell - Chinese booster box search",
        url="https://www.carousell.sg/search/pokemon%20chinese%20booster%20box?sort_by=3",
        parser="carousell",
        runs_on="local",
    ),
    Target(
        name="Carousell - Elite Trainer Box search",
        url="https://www.carousell.sg/search/pokemon%20elite%20trainer%20box?sort_by=3",
        parser="carousell",
        runs_on="local",
    ),
    Target(
        name="Carousell - Booster bundle search",
        url="https://www.carousell.sg/search/pokemon%20booster%20bundle?sort_by=3",
        parser="carousell",
        runs_on="local",
    ),
    Target(
        name="Carousell - Premium collection search",
        url="https://www.carousell.sg/search/pokemon%20premium%20collection?sort_by=3",
        parser="carousell",
        runs_on="local",
    ),
]


# ======================================================================
# Buyout-lot alerts (local mode only). Collections, binders and bulk lots
# sold at one price can't be priced automatically, so the job here is
# SPEED: tell Kenny within one poll that a new lot was posted, so he can
# look before other dealers do. Newest-first Carousell searches.
# ======================================================================
BUYOUT_SEARCHES = [
    ("pokemon binder", "https://www.carousell.sg/search/pokemon%20binder?sort_by=3"),
    ("pokemon collection", "https://www.carousell.sg/search/pokemon%20card%20collection?sort_by=3"),
    ("old pokemon cards", "https://www.carousell.sg/search/old%20pokemon%20cards?sort_by=3"),
    ("pokemon cards lot", "https://www.carousell.sg/search/pokemon%20cards%20lot?sort_by=3"),
    ("pokemon cards clearance", "https://www.carousell.sg/search/pokemon%20cards%20clearance?sort_by=3"),
]
BUYOUT_MIN_PRICE = 20
BUYOUT_MAX_PRICE = 400  # above the usual buying cap even with a discount

# Tuned against 103 real listings from five newest-first searches (5 Nov
# 2026). Most results for these searches are NOT lots: empty binders,
# boxed "Binder Collection" products, single cards from sets named
# "... Collection", and dealers' keyword-stuffed titles.
#
# A lot title must clearly say it's a lot...
_LOT_STRONG = re.compile(
    r"\b(lot|lots|bulk|whole|entire|childhood|clearance|clearing|letting go|moving out|"
    r"quit|quitting|all for|bundle of|\d{2,4}\s*\+?\s*(pcs|cards)|"
    r"(my|whole|entire|full|card|cards) collection|collection (lot|sale|clearance)|"
    r"(old|vintage|random|assorted) (pok[eé]mon )?cards)\b",
    re.I,
)
# ...or be a binder/album that says it has cards in it...
_LOT_CONTAINER = re.compile(r"\b(binder|album|folder)\b", re.I)
_HAS_CARDS = re.compile(r"\b(cards|full|filled|with cards|inside)\b", re.I)
# ...and mention none of these (accessories, wanted ads, graded, sealed
# product, pre-orders).
_NOT_A_LOT = re.compile(
    r"\b(wtb|wtt|buying|buy all|buyback|looking for|sleeves?|toploaders?|top ?loaders?|"
    r"deck ?box|playmat|magnetic|empty|binder only|zip|\d+ ?pockets?|\d+ ?slots?|"
    r"ring (card )?binder|psa|bgs|cgc|graded|slab|booster|boosters|etb|elite trainer|"
    r"sealed|figure|plush|premium collection|special collection|binder collection|"
    r"collection box|ultra premium|upc|tin|blister|pack|packs|leather|oversize|"
    r"mini binder|trading card (game )?binders?|card holder|storage|case|"
    r"cash-?out|preorder|pre-order|po)\b",
    re.I,
)
# Dealers stuff titles with set names to catch every search; a real lot
# rarely names 3+ sets.
_SET_NAMES = re.compile(
    r"\b(151|prismatic|terr?astal|terestal|mega brave|mega symphonia|mega dream|"
    r"ascended heroes|perfect order|phantasmal|chaos rising|pitch black|inferno x|"
    r"abyss eye|ninja spinner|glory of team rocket|vstar universe|black bolt|"
    r"white flare|destined rivals|journey together|evolving skies|shiny treasure|"
    r"crown zenith|lost origin|silver tempest|25th|30th|surging sparks|stellar crown)\b",
    re.I,
)


def names_many_sets(title: str) -> bool:
    """True for titles naming 3+ sets: a dealer's price list ("Japanese
    booster box Abyss Eye / Ninja Spinner / Mega Dream / ...") with one
    "from" price, or keyword stuffing. Neither can be priced as one product."""
    return len({m.lower() for m in _SET_NAMES.findall(title)}) >= 3


def is_buyout_lot(title: str, price: float | None) -> bool:
    if price is None or not (BUYOUT_MIN_PRICE <= price <= BUYOUT_MAX_PRICE):
        return False
    if not re.search(r"pok[eé]mon", title, re.I):
        return False
    if _NOT_A_LOT.search(title):
        return False
    if names_many_sets(title):
        return False
    return bool(_LOT_STRONG.search(title)) or bool(
        _LOT_CONTAINER.search(title) and _HAS_CARDS.search(title)
    )


def _listing_id(url: str) -> str:
    m = re.search(r"(\d{6,})/?$", _clean_url(url))
    return m.group(1) if m else _clean_url(url)


def _build_lot_message(item: dict, search: str) -> str:
    title = html.escape(item["title"])
    url = html.escape(_clean_url(item["url"]), quote=True)
    return (
        f"\U0001F0CF <b>New lot: {title}</b>\n"
        f"S${item['price']:.0f} · found by \"{html.escape(search)}\"\n"
        f"<a href=\"{url}\">View listing</a> · send to Claude to price-check"
    )


def run_buyout_searches(state: dict, fetch=None) -> None:
    """Alert once per new lot listing. The first time a search runs it only
    records what's already there (no alert flood); after that, only
    listings posted since are sent."""
    fetch = fetch or (lambda url: check_carousell(Target(name="buyout", url=url, parser="carousell")))
    lots_state = state.setdefault("lots", {})
    for search, url in BUYOUT_SEARCHES:
        try:
            listings = fetch(url)
        except Exception as exc:  # noqa: BLE001
            print(f"[error] buyout search '{search}': {exc}")
            continue
        seen = set(lots_state.get(search, []))
        first_run = search not in lots_state
        lots = [x for x in listings if x.get("in_stock", True) and is_buyout_lot(x["title"], x.get("price"))]
        new = [x for x in lots if _listing_id(x["url"]) not in seen]
        print(f"[lots] {search}: {len(listings)} listings, {len(lots)} lots, {0 if first_run else len(new)} new")
        for item in new:
            lid = _listing_id(item["url"])
            if not first_run:
                # Another search may already have sent this listing this run.
                if any(lid in v for k, v in lots_state.items() if k != search):
                    seen.add(lid)
                    continue
                try:
                    send_telegram(_build_lot_message(item, search))
                except Exception as exc:  # noqa: BLE001
                    print(f"[error] failed to send lot alert {lid}: {exc}")
                    continue  # not marked seen, so retried next run
            seen.add(lid)
        # Keep the list bounded: newest-first searches only ever show the
        # latest few dozen, so 500 ids is plenty of memory.
        lots_state[search] = sorted(seen, reverse=True)[:500]
        save_state(state)


# ======================================================================
# State persistence (dedup for individual alerts + digest change-detection)
# ======================================================================
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"seen": {}, "digests": {}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _clean_url(url: str) -> str:
    """Strip query string and fragment so the URL is stable for dedup.
    Caught live in a prior build: Carousell embeds a fresh per-request
    tracking token (?t-id=...&t-referrer_request_id=...) in every listing
    URL, so the same physical listing got a different dedup key - and
    therefore re-alerted as "new" - on every single run."""
    if not url:
        return url
    return url.split("?", 1)[0].split("#", 1)[0]


# ======================================================================
# Central listing processing (PRD §8.4-8.6): classify, detect language,
# match against the right catalog, compute pct_above.
# ======================================================================
def process_listing(raw: dict, catalogs: dict[str, list[dict]]) -> dict | None:
    if not raw.get("in_stock", True):
        return None

    title = raw["title"]
    category = classify(title)
    if category is None:
        return None

    # Caught live 5 Nov 2026: a dealer's list of a dozen Japanese boxes at a
    # "from" S$85 price matched one set and alerted as below MSRP.
    if names_many_sets(title):
        return None

    language = detect_language(title)
    catalog = catalogs.get(language, [])
    match = find_catalog_match(title, category, catalog)
    if match is None:
        return None

    price = raw["price"]
    reference_price = match["price"]
    pct_above = round((price - reference_price) / reference_price * 100, 1)

    return {
        "title": title,
        "category": category,
        "language": language,
        "price": price,
        "reference_price": reference_price,
        "pct_above": pct_above,
        "url": _clean_url(raw["url"]),
        "retailer": raw["target_name"],
    }


def classify_deal(parser_name: str, pct_above: float, language: str = "english", resale_deal_pct: float = 80.0) -> str:
    """Returns "deal" (individual alert), "above" (digest), or "skip".

    Lazada IS the English reference-price source (§7.1) - when checked as
    an ordinary retailer too, its own listings trivially equal their own
    catalog price (pct_above == 0) on every run, since both sides come
    from the same live scrape. That's not a deal (not a discount) and not
    "above MSRP" either (it IS the MSRP) - caught live during e2e testing,
    where this flooded 28 individual "deal" alerts on the very first run.
    Only a STRICT discount below its own reference is a genuine, alertable
    deal for Lazada. Other retailers keep <= 0 as a deal, since an
    independent seller genuinely landing exactly at MSRP is a real
    coincidence worth flagging.

    Chinese references are local RESALE prices, not MSRP, so a listing at
    the reference is merely fair. It's a deal only at or below
    resale_deal_pct of resale (e.g. 80%), leaving room to flip it."""
    if language == "chinese":
        return "deal" if (100 + pct_above) <= resale_deal_pct else "above"
    if parser_name == "lazada":
        if pct_above == 0:
            return "skip"
        return "deal" if pct_above < 0 else "above"
    return "deal" if pct_above <= 0 else "above"


# ======================================================================
# Telegram (PRD §8.7)
# ======================================================================
def send_telegram(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[warn] Telegram not configured, would have sent:\n", message)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=15,
    )
    resp.raise_for_status()


LANG_LABELS = {
    "english": "English/International",
    "japanese": "Japanese import",
    "chinese": "Simplified Chinese",
}


def _build_individual_message(item: dict) -> str:
    # Telegram's HTML parse_mode treats unescaped <, >, & as markup - a raw
    # listing title containing e.g. "<Confirmed 17 May>" broke a prior
    # build's Telegram send with a 400 error. Escape everything interpolated,
    # including the URL used in the href="..." attribute.
    title = html.escape(item["title"])
    category = html.escape(item["category"])
    retailer = html.escape(item["retailer"])
    url = html.escape(item["url"], quote=True)
    lang_label = LANG_LABELS.get(item["language"], "English/International")
    if item["language"] == "chinese":
        pct_of_ref = 100 + item["pct_above"]
        return (
            f"\U0001F4B0 <b>Below resale: {title}</b>\n"
            f"Category: {category} ({lang_label})\n"
            f"Retailer: {retailer}\n"
            f"Price: SGD {item['price']:.2f} = {pct_of_ref:.0f}% of SGD {item['reference_price']:.2f} resale\n"
            f"Check delivery fee and quantity before buying.\n"
            f"<a href=\"{url}\">View listing</a>"
        )
    return (
        f"\U0001F6CD️ <b>At/below MSRP: {title}</b>\n"
        f"Category: {category} ({lang_label})\n"
        f"Retailer: {retailer}\n"
        f"Price: SGD {item['price']:.2f} (ref. SGD {item['reference_price']:.2f})\n"
        f"<a href=\"{url}\">View listing</a>"
    )


def _build_digest_message(retailer: str, items: list[dict]) -> str:
    items_sorted = sorted(items, key=lambda i: i["pct_above"])
    shown = items_sorted[:DIGEST_TOP_N]
    remaining = len(items_sorted) - len(shown)

    retailer_esc = html.escape(retailer)
    lines = [f"\U0001F4CA <b>Above MSRP digest: {retailer_esc}</b>\n"]
    for item in shown:
        title = html.escape(item["title"])
        url = html.escape(item["url"], quote=True)
        # Chinese references are resale prices, and a Chinese listing can sit
        # in the digest while below its reference (cheaper than resale but
        # not cheap enough to flip), so label the reference and sign the %.
        ref_label = "resale" if item.get("language") == "chinese" else "ref."
        lines.append(
            f"• <a href=\"{url}\">{title}</a>\n"
            f"  SGD {item['price']:.2f} ({ref_label} SGD {item['reference_price']:.2f}, "
            f"{item['pct_above']:+.1f}%)"
        )
    if remaining > 0:
        lines.append(f"\n+{remaining} more above MSRP at this retailer.")
    return "\n".join(lines)


def _digest_signature(items: list[dict]) -> str:
    """Stable fingerprint of a digest's content, for change-detection
    (PRD §8.10) - only re-send when this actually changes."""
    key_parts = sorted(f"{i['url']}:{i['price']}" for i in items)
    return "|".join(key_parts)


# ======================================================================
# Main run (PRD §8)
# ======================================================================
def _build_resale_message(item: dict, official: bool = False) -> str:
    title = html.escape(item["title"])
    url = html.escape(item["url"], quote=True)
    meta = html.escape(
        f"{LANG_LABELS.get(item['language'], 'English')} · {item['category']} · {item['retailer']}"
    )
    priced_as = html.escape(f"{item['market_title']} ({item['source']})")
    price, mkt, pct = item["price"], item["market_sgd"], item["pct_of_market"]
    if item["verdict"] == "check":
        head = f"\u26A0\uFE0F <b>Check: {title}</b>"
        body = (
            f"S${price:.2f} is only {pct}% of market S${mkt:.2f}. Probably a different product, "
            f"a variant's price or a scam, but worth a look."
        )
    else:
        head = (f"\U0001F6CD\uFE0F <b>Official store in stock: {title}</b>" if official
                else f"\U0001F4B0 <b>Deal: {title}</b>")
        body = f"S${price:.2f} = {pct}% of market S${mkt:.2f} (S${mkt - price:.0f} below market)"
        if not official:
            body += "\nCheck delivery fee, quantity and condition before buying."
    return f"{head}\n{meta}\n{body}\nPriced as: {priced_as}\n<a href=\"{url}\">View listing</a>"


def _run_all_targets(state: dict) -> None:
    chinese_catalog, chinese_deal_pct = load_chinese_prices()
    market, usd_sgd = get_market_catalogs()
    print(
        f"[market] english={len(market['english'])} sealed products, japanese={len(market['japanese'])}, "
        f"chinese resale list={len(chinese_catalog)}, USD->SGD {usd_sgd:.4f}; "
        f"deal <= {DEAL_PCT_OF_MARKET}% of market, check < {CHECK_BELOW_PCT_OF_MARKET}%"
    )

    for target in TARGETS:
        if target.runs_on != MODE:
            continue
        parser_fn = PARSERS.get(target.parser)
        if not parser_fn:
            print(f"[skip] no parser for {target.name}")
            continue
        try:
            raw_listings = parser_fn(target)
        except Exception as exc:  # noqa: BLE001
            print(f"[error] {target.name}: {exc}")
            continue

        official = target.parser == "lazada"
        priced = deals = checks = 0
        alertable_now = set()

        for raw in raw_listings:
            item = evaluate_listing(raw, market, chinese_catalog, usd_sgd, chinese_deal_pct)
            if item is None:
                continue
            priced += 1
            if official and item["verdict"] == "check":
                # The official store at retail far below market is a hot
                # restock, not a mismatch or a scam.
                item["verdict"] = "deal"
            if item["verdict"] == "none":
                continue
            deals += item["verdict"] == "deal"
            checks += item["verdict"] == "check"

            key = f"{item['retailer']}:{item['url']}"
            if official and state["seen"].get(key) != item["price"] and not lazada_product_in_stock(item["url"]):
                # Sold out: not alertable now, so the restock alerts later.
                deals -= item["verdict"] == "deal"
                continue
            alertable_now.add(key)
            if state["seen"].get(key) == item["price"]:
                continue  # already alerted at this price
            # The same Carousell listing often shows up in two searches;
            # alert once per listing and price, not once per search.
            if any(k.endswith(":" + item["url"]) and v == item["price"] for k, v in state["seen"].items()):
                state["seen"][key] = item["price"]
                continue

            try:
                send_telegram(_build_resale_message(item, official=official))
            except Exception as exc:  # noqa: BLE001
                # Not marked as seen, so it's retried next run.
                print(f"[error] failed to send alert for {key}: {exc}")
                continue
            state["seen"][key] = item["price"]
            print(f"[alert] {item['verdict']} {key} @ {item['price']} = {item['pct_of_market']}% of market")

        # Shops (not Carousell): forget items that sold out or stopped
        # qualifying, so a later restock alerts again.
        if target.parser != "carousell":
            prefix = f"{target.name}:"
            for k in [k for k in state["seen"] if k.startswith(prefix) and k not in alertable_now]:
                del state["seen"][k]

        # One line per target so a site that silently returns nothing (blocked,
        # markup changed) is visible in the run log.
        print(f"[target] {target.name}: {len(raw_listings)} listings, {priced} priced, "
              f"{deals} deals, {checks} to check")
        save_state(state)  # persist after each target, not just at the very end


def run() -> None:
    state = load_state()
    state.setdefault("seen", {})
    state.setdefault("digests", {})
    print(f"[mode] {MODE}")
    try:
        _run_all_targets(state)
        if MODE == "local":
            run_buyout_searches(state)
            import sell_digest  # Mac only: daily sell-side price check

            sell_digest.run_sell_digest(state)
    finally:
        save_state(state)  # belt-and-suspenders: never lose progress


if __name__ == "__main__":
    run()
