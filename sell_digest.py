"""
Daily sell-side price check (Mac only, Nov 2026).

Once a day, for every sealed product Kenny holds to sell (tracker holdings,
quantity > 0, not marked Never Sell), compare:
  - his own Carousell ask (from his profile, @ikayley),
  - the lowest ask from other Carousell sellers for the same product,
  - the market value (the tracker's daily TCGplayer price, in SGD),
and send ONE Telegram message saying what to do: cut, raise, list, or
leave it. Read-only everywhere: it never changes a listing or the tracker.

Runs from run() in local mode once the Mac's local time is past
SELL_DIGEST_HOUR and it hasn't been sent today.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import sys
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

import pokemon_msrp_watcher as w

TRACKER_STORAGE_URL = "https://kenny-tcg-tracker.vercel.app/api/storage"
CAROUSELL_PROFILE_URL = "https://www.carousell.sg/u/ikayley/"
SELL_DIGEST_HOUR = 9  # local time on the Mac
MAX_LINES = 15
UNDERCUT_MARGIN = 1.05  # your ask more than 5% above the lowest other ask
UNDERPRICED_MARGIN = 0.90  # your ask more than 10% below the lowest other ask
# Competing asks under this share of market aren't real offers: they're
# placeholders, deposits or stale listings (caught live 10 Nov 2026: a 2022
# "S$205 Silver Tempest Booster Box" listing against a S$646 market).
MIN_OTHER_PCT_OF_MARKET = 0.5
# Carousell listing IDs grow over time (~5-6 million a month). An ask whose
# ID is this far below the newest one seen in the run is roughly six months
# old or more: usually sold-out stock nobody took down, at an old price.
STALE_LISTING_ID_GAP = 35_000_000

# Tracker category -> watcher category (see CATEGORIES in the watcher).
TRACKER_CATEGORIES = {
    "Booster Box": "Booster box",
    "Elite Trainer Box": "Elite Trainer Box",
    "Booster Bundle": "Booster bundle",
    "Ultra Premium Collection": "Premium/Special collection",
    "Super-Premium Collection": "Premium/Special collection",
    "Tin": "Tin",
}


def tracker_password() -> str:
    pw = os.environ.get("TRACKER_PASSWORD", "")
    if not pw and w.SECRETS_FILE.exists():
        pw = json.loads(w.SECRETS_FILE.read_text()).get("tracker_password", "")
    return pw


def fetch_holdings(password: str) -> list[dict]:
    resp = requests.get(
        TRACKER_STORAGE_URL, params={"key": "holdings"}, headers={"x-app-password": password}, timeout=30
    )
    resp.raise_for_status()
    return json.loads(resp.json()["value"])


# ----------------------------------------------------------------------
# Pure logic (unit-tested)
# ----------------------------------------------------------------------
def holding_products(holdings: list[dict], usd_sgd: float) -> list[dict]:
    """Group sellable holdings into products the matcher can recognise:
    {key, title, category, language, quantity, market_sgd}."""
    products: dict[str, dict] = {}
    for h in holdings or []:
        if h.get("neverSell") or (h.get("quantity") or 0) <= 0:
            continue
        name = (h.get("setName") or h.get("name") or "").strip()
        variant = (h.get("variant") or "").strip()
        tracker_cat = h.get("category") or ""
        if tracker_cat == "Other":
            category = w.classify(f"pokemon {variant}") if variant else None
            title = f"{name} {variant}"
        else:
            category = TRACKER_CATEGORIES.get(tracker_cat)
            title = f"{name} {variant} {tracker_cat}".replace("  ", " ")
        if not name or not category:
            continue
        key = f"{name.lower()}|{variant.lower()}|{tracker_cat.lower()}"
        p = products.setdefault(key, {
            "key": key,
            "title": title.strip(),
            "category": category,
            "language": w.detect_language(f"{name} {variant}"),
            "quantity": 0,
            "market_sgd": round(float(h.get("currentPrice") or 0) * usd_sgd, 2),
        })
        p["quantity"] += int(h.get("quantity") or 0)
    return list(products.values())


def _market_entry(title: str, category: str, language: str, market: dict | None) -> dict | None:
    if not market or language not in market:
        return None
    return w.find_market_match(title, category, market[language])


def attach_market_entries(products: list[dict], market: dict | None) -> None:
    """Identify each held product as one TCGplayer product, so listings can
    be compared on identity (most specific match among ~950 products)
    rather than word overlap with this one holding."""
    for p in products:
        p["_entry"] = _market_entry(p["title"], p["category"], p["language"], market)


def match_product(listing_title: str, products: list[dict], market: dict | None = None) -> dict | None:
    """Which held product (if any) a Carousell listing is.

    With the market catalog: the listing and the holding must resolve to
    the SAME TCGplayer product (caught on real data: a base "Scarlet &
    Violet ETB" holding swallowed Destined Rivals and Paldean Fates ETB
    listings, whose titles also say "Scarlet & Violet"). Without it, or for
    holdings TCGplayer doesn't list, fall back to direct matching."""
    if w._NOT_SEALED_LISTING.search(listing_title):
        return None
    category = w.classify(listing_title)
    if not category:
        return None
    language = w.detect_language(listing_title)
    same_lang = [p for p in products if p["language"] == language]

    listing_entry = _market_entry(listing_title, category, language, market)
    if listing_entry is not None:
        # A known TCGplayer product: it's a holding only if that holding is
        # the same product.
        return next((p for p in same_lang if p.get("_entry") is listing_entry), None)
    unidentified = [p for p in same_lang if p.get("_entry") is None]
    entries = [{"title": p["title"], "category": p["category"], "usd": 0, "_product": p} for p in unidentified]
    m = w.find_market_match(listing_title, category, entries)
    return m["_product"] if m else None


def advise(product: dict, my_ask: float | None, lowest_other: float | None) -> tuple[str, str]:
    """(action, text). Actions sort by urgency: cut, list, raise, ok."""
    mkt = product["market_sgd"]
    if my_ask is None:
        if lowest_other:
            target = min(lowest_other - 1, mkt) if mkt else lowest_other - 1
            return "list", f"not listed. List at ~S${target:.0f}"
        if mkt:
            return "list", f"not listed, no other sellers. List at ~S${mkt:.0f}"
        return "ok", "not listed"
    if lowest_other and my_ask > lowest_other * UNDERCUT_MARGIN:
        return "cut", f"undercut. Cut to ~S${lowest_other - 1:.0f} or hold"
    if lowest_other and my_ask < lowest_other * UNDERPRICED_MARGIN and (not mkt or my_ask < mkt):
        target = min(lowest_other - 1, mkt) if mkt else lowest_other - 1
        return "raise", f"cheapest by far. Room to raise to ~S${target:.0f}"
    return "ok", "priced right"


_ORDER = {"cut": 0, "list": 1, "raise": 2, "ok": 3}
_ICON = {"cut": "\U0001F53B", "raise": "\U0001F53A"}
TOP_UNLISTED = 5


def build_digest(rows: list[dict], today: dt.date) -> str:
    """rows: {product, my_ask, lowest_other, n_other, action, text}.

    Two sections: what to change on the listings Kenny has up, and the few
    unlisted holdings worth listing now (most valuable first). Everything
    else is a count, so the message stays short when most stock isn't
    listed (the case on 5 Nov 2026: 2 of ~45 sellable products listed)."""
    listed = [r for r in rows if r["my_ask"] is not None]
    unlisted = [r for r in rows if r["my_ask"] is None]
    changes = sorted([r for r in listed if r["action"] in ("cut", "raise")],
                     key=lambda r: (_ORDER[r["action"]], -r["product"]["market_sgd"]))
    fine = len(listed) - len(changes)

    def value(r):
        return r["lowest_other"] or r["product"]["market_sgd"] or 0

    worth = sorted([r for r in unlisted if value(r)], key=value, reverse=True)[:TOP_UNLISTED]

    lines = [f"\U0001F4CB <b>Daily price check · {today:%a %d %b}</b>", ""]
    lines.append(f"<b>Your listings</b> ({len(listed)})")
    if not listed:
        lines.append("None of your holdings are listed.")
    for r in changes[:MAX_LINES]:
        p = r["product"]
        low = (f"lowest other S${r['lowest_other']:.0f} ({r['n_other']})" if r["lowest_other"] is not None
               else "no other sellers")
        mkt = f" · market S${p['market_sgd']:.0f}" if p["market_sgd"] else ""
        lines.append(
            f"{_ICON[r['action']]} {html.escape(p['title'])}: you S${r['my_ask']:.0f} · {low}{mkt}\n"
            f"   → {html.escape(r['text'])}"
        )
    if fine:
        lines.append(f"\u2705 {fine} priced in line with Carousell" if changes else
                     f"\u2705 All {fine} priced in line with Carousell")

    if worth:
        lines += ["", f"<b>Worth listing</b> (top {len(worth)} of {len(unlisted)} not listed)"]
        for r in worth:
            p = r["product"]
            low = f"lowest S${r['lowest_other']:.0f} ({r['n_other']})" if r["lowest_other"] else "no other sellers"
            mkt = f" · market S${p['market_sgd']:.0f}" if p["market_sgd"] else ""
            lines.append(f"• {html.escape(p['title'])} ×{p['quantity']}: {low}{mkt}")
    return "\n".join(lines)


def digest_due(state: dict, now: dt.datetime) -> bool:
    return now.hour >= SELL_DIGEST_HOUR and state.get("sell_digest_date") != now.date().isoformat()


# ----------------------------------------------------------------------
# Carousell fetching (one browser for all pages)
# ----------------------------------------------------------------------
def _parse_cards(html_content: str, base_url: str) -> list[dict]:
    soup = BeautifulSoup(html_content, "html.parser")
    out, seen = [], set()
    for link in soup.select('a[href^="/p/"]'):
        href = link.get("href")
        if not href or href in seen:
            continue
        seen.add(href)
        img = link.select_one("img[alt]")
        text = link.get_text(" ", strip=True)
        m = re.search(r"S\$\s?([\d,]+(?:\.\d{2})?)", text)
        if not img or not img.get("alt") or not m:
            continue
        if any(x in text.lower() for x in ("sold", "reserved")):
            continue
        out.append({
            "title": img["alt"],
            "price": float(m.group(1).replace(",", "")),
            "url": w._clean_url("https://www.carousell.sg" + href),
        })
    return out


def fetch_carousell_pages(search_urls: list[str]) -> tuple[list[dict], dict[str, list[dict]]]:
    """Returns (own profile listings, {search url: listings})."""
    from playwright.sync_api import sync_playwright

    results: dict[str, list[dict]] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(user_agent=w.HEADERS["User-Agent"])
            page.goto(CAROUSELL_PROFILE_URL, timeout=30000, wait_until="load")
            page.wait_for_timeout(4000)
            for _ in range(15):  # "View more" until everything is loaded
                button = page.get_by_role("button", name=re.compile(r"view more|load more", re.I))
                if button.count() == 0:
                    break
                button.first.click()
                page.wait_for_timeout(2500)
            own = _parse_cards(page.content(), CAROUSELL_PROFILE_URL)
            for url in search_urls:
                try:
                    page.goto(url, timeout=30000, wait_until="load")
                    page.wait_for_timeout(3000)
                    results[url] = _parse_cards(page.content(), url)
                except Exception as exc:  # noqa: BLE001
                    print(f"[warn] sell check search failed: {exc}", file=sys.stderr)
                    results[url] = []
        finally:
            browser.close()
    return own, results


def search_url(product: dict) -> str:
    q = f"pokemon {product['title']}"
    if product["language"] == "japanese" and "japanese" not in q.lower():
        q += " japanese"
    return "https://www.carousell.sg/search/" + quote(q)


def _numeric_id(url: str) -> int:
    lid = w._listing_id(url)
    return int(lid) if lid.isdigit() else 0


def is_stale(url: str, newest_id: int) -> bool:
    lid = _numeric_id(url)
    return bool(lid and newest_id and newest_id - lid > STALE_LISTING_ID_GAP)


def run_sell_digest(state: dict, now: dt.datetime | None = None) -> None:
    now = now or dt.datetime.now()
    if not digest_due(state, now):
        return
    password = tracker_password()
    if not password:
        print("[sell] skipped: no tracker_password in secrets.json (run mac/add_tracker_password.sh)")
        return
    try:
        holdings = fetch_holdings(password)
    except Exception as exc:  # noqa: BLE001
        print(f"[error] sell check: couldn't read tracker holdings: {exc}")
        return
    market, usd_sgd = w.get_market_catalogs()
    products = holding_products(holdings, usd_sgd)
    attach_market_entries(products, market)
    if not products:
        print("[sell] no sellable holdings")
        return

    urls = {p["key"]: search_url(p) for p in products}
    own, searches = fetch_carousell_pages(list(urls.values()))
    own_ids = {x["url"] for x in own}

    my_asks: dict[str, float] = {}
    for listing in own:
        p = match_product(listing["title"], products, market)
        if p:
            my_asks[p["key"]] = min(listing["price"], my_asks.get(p["key"], float("inf")))

    newest_id = max((_numeric_id(x["url"]) for res in searches.values() for x in res), default=0)
    rows = []
    for p in products:
        others = [
            x["price"] for x in searches.get(urls[p["key"]], [])
            if x["url"] not in own_ids and match_product(x["title"], [p], market) is p
            # ignore obvious junk prices (cases, deposits, S$1 placeholders)
            and (not p["market_sgd"]
                 or MIN_OTHER_PCT_OF_MARKET * p["market_sgd"] <= x["price"] <= 3 * p["market_sgd"])
            and not is_stale(x["url"], newest_id)
        ]
        lowest = min(others) if others else None
        action, text = advise(p, my_asks.get(p["key"]), lowest)
        rows.append({"product": p, "my_ask": my_asks.get(p["key"]), "lowest_other": lowest,
                     "n_other": len(others), "action": action, "text": text})

    print(f"[sell] {len(products)} products, {len(own)} of your listings ({len(my_asks)} matched), "
          + ", ".join(f"{a} {sum(r['action'] == a for r in rows)}" for a in _ORDER))
    try:
        w.send_telegram(build_digest(rows, now.date()))
    except Exception as exc:  # noqa: BLE001
        print(f"[error] failed to send sell check: {exc}")
        return
    state["sell_digest_date"] = now.date().isoformat()
    w.save_state(state)
