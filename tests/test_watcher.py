"""
Unit tests for the classification, language-detection, fuzzy-matching, and
notification logic - no network calls. Several of these are regression
tests for real bugs caught live during development (see comments).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import pokemon_msrp_watcher as w  # noqa: E402


# ---- classify() ----------------------------------------------------------
def test_classify_word_boundary_and_plurals():
    assert w.classify("Pokemon Destiny Deck Box") is None  # "tin" must not match inside "destiny"
    assert w.classify("Pokemon Satin Finish Playmat") is None
    assert w.classify("Pokemon Scarlet & Violet Tin") == "Tin"
    assert w.classify("Pokemon Scarlet & Violet Tins (2-pack)") == "Tin"
    assert w.classify("Pokemon TCG Chaos Rising 6 Booster Boxes") == "Booster box"
    assert w.classify("Random Toy Story Figure") is None  # no "pokemon" mention


def test_classify_checks_most_specific_category_first():
    assert w.classify("Pokemon Booster Bundle") == "Booster bundle"
    assert w.classify("Pokemon Single Booster Pack") == "Single booster pack"


def test_classify_booster_display_box():
    # Regression: "Display" sitting between "Booster" and "Box" broke the
    # exact-phrase match and fell through to the generic "booster" ->
    # "Single booster pack" fallback, misclassifying a real box listing.
    assert w.classify("Pokemon Mega Evolution: Perfect Order Boosters Display Box") == "Booster box"
    assert w.classify("Pokemon Chaos Rising Booster Display Box") == "Booster box"


# ---- detect_language() ----------------------------------------------------
def test_detect_language():
    assert w.detect_language("Pokemon TCG - Abyss Eye Japanese Booster Box") == "japanese"
    assert w.detect_language("Pokemon TCG: MEGA Inferno X (m2) JP Booster Box") == "japanese"
    assert w.detect_language("Pokemon Trading Card Game: Chaos Rising Elite Trainer Box") == "english"


# ---- find_catalog_match() --------------------------------------------------
JP_CATALOG = [
    {"title": "Abyss Eye", "category": "Booster box", "price": 48.24},
    {"title": "Black Bolt", "category": "Booster box", "price": 69.95},
]
ENG_CATALOG = [
    {
        "title": "Pokémon Trading Card Game: Mega Evolution: Chaos Rising Pokémon Center Elite Trainer Box [Limit 2 per person]",
        "category": "Elite Trainer Box",
        "price": 109.90,
    },
]


def test_find_catalog_match_substring_case():
    # Regression: plain SequenceMatcher.ratio() on full strings scored this
    # ~0.30 (listing has lots of extra words) despite "abyss eye" being a
    # clean substring - the fix uses substring containment / word-recall
    # after stripping shared boilerplate.
    match = w.find_catalog_match(
        "Pokemon TCG – Abyss Eye Japanese Booster Box Shrinked", "Booster box", JP_CATALOG
    )
    assert match is not None
    assert match["title"] == "Abyss Eye"
    assert match["price"] == 48.24


def test_find_catalog_match_noisy_catalog_entry():
    # Regression: the CATALOG entry itself was noisy ("[Limit 2 per
    # person]", "Pokémon Center"), which the original word-recall scoring
    # assumed only happened on the listing side - fixed by stripping known
    # boilerplate from both sides before comparing.
    match = w.find_catalog_match(
        "Pokemon Center Mega Evolution: Chaos Rising Elite Trainer Box",
        "Elite Trainer Box",
        ENG_CATALOG,
    )
    assert match is not None
    assert match["price"] == 109.90


def test_find_catalog_match_standard_etb_does_not_match_pokemon_center_etb():
    # Regression (Nov 2026): a S$89.90 Standard Perfect Order ETB matched the
    # S$109.90 Pokemon Center one and alerted as 18% below MSRP.
    assert w.find_catalog_match(
        "Pokemon Trading Card Game: Mega Evolution: Chaos Rising Elite Trainer Box",
        "Elite Trainer Box",
        ENG_CATALOG,
    ) is None

def test_find_catalog_match_rejects_unrelated():
    match = w.find_catalog_match(
        "Pokemon TCG Totally Different Made Up Set Booster Box", "Booster box", JP_CATALOG
    )
    assert match is None


def test_find_catalog_match_category_must_match_exactly():
    # An ETB listing must never match a Booster box catalog entry, even if
    # the set name would otherwise be similar.
    match = w.find_catalog_match("Pokemon Abyss Eye Elite Trainer Box", "Elite Trainer Box", JP_CATALOG)
    assert match is None


# ---- _clean_url() ----------------------------------------------------------
def test_clean_url_strips_tracking_params_for_dedup_stability():
    # Regression: Carousell embeds a fresh per-request tracking token in
    # every listing URL, so the same physical listing got a different dedup
    # key - and re-alerted as "new" - on every single run.
    url1 = "https://www.carousell.sg/p/some-listing-123/?t-id=AAA&t-referrer_request_id=111"
    url2 = "https://www.carousell.sg/p/some-listing-123/?t-id=BBB&t-referrer_request_id=222"
    assert w._clean_url(url1) == w._clean_url(url2) == "https://www.carousell.sg/p/some-listing-123/"


# ---- process_listing() -----------------------------------------------------
CATALOGS = {"english": ENG_CATALOG, "japanese": JP_CATALOG}


def test_process_listing_at_or_below_msrp():
    raw = {
        "title": "Pokemon TCG – Abyss Eye Japanese Booster Box Shrinked",
        "price": 40.0,
        "url": "https://example.com/a?tracking=1",
        "in_stock": True,
        "target_name": "Test Retailer",
    }
    item = w.process_listing(raw, CATALOGS)
    assert item is not None
    assert item["pct_above"] < 0
    assert item["reference_price"] == 48.24
    assert item["url"] == "https://example.com/a"  # tracking param stripped


def test_process_listing_above_msrp():
    raw = {
        "title": "Pokemon TCG – Abyss Eye Japanese Booster Box Shrinked",
        "price": 100.0,
        "url": "https://example.com/b",
        "in_stock": True,
        "target_name": "Test Retailer",
    }
    item = w.process_listing(raw, CATALOGS)
    assert item is not None
    assert item["pct_above"] > 0


def test_process_listing_out_of_stock_returns_none():
    raw = {
        "title": "Pokemon TCG – Abyss Eye Japanese Booster Box",
        "price": 10.0,
        "url": "https://example.com/c",
        "in_stock": False,
        "target_name": "Test Retailer",
    }
    assert w.process_listing(raw, CATALOGS) is None


def test_process_listing_no_category_match_returns_none():
    raw = {
        "title": "Pokemon Plush Toy Figure",
        "price": 10.0,
        "url": "https://example.com/d",
        "in_stock": True,
        "target_name": "Test Retailer",
    }
    assert w.process_listing(raw, CATALOGS) is None


def test_process_listing_no_catalog_match_returns_none():
    raw = {
        "title": "Pokemon TCG Totally Unrelated Set Booster Box",
        "price": 10.0,
        "url": "https://example.com/e",
        "in_stock": True,
        "target_name": "Test Retailer",
    }
    assert w.process_listing(raw, CATALOGS) is None


# ---- Telegram message building (HTML escaping) -----------------------------
def test_individual_message_escapes_html_special_chars():
    # Regression: a real listing titled "<Confirmed 17 May> Booster Box..."
    # crashed a prior build's Telegram send with a 400 error (unescaped <>
    # parsed as markup).
    item = {
        "title": "<Confirmed 17 May> Booster Box Pitch Black Pokemon",
        "category": "Booster box",
        "language": "japanese",
        "price": 90.0,
        "reference_price": 100.0,
        "url": "https://example.com/f?a=1&b=2",
        "retailer": "Test & Co",
    }
    message = w._build_individual_message(item)
    assert "<Confirmed" not in message  # raw unescaped tag must not appear
    assert "&lt;Confirmed" in message
    assert "&amp;" in message  # url's raw & must be escaped too


def test_digest_message_caps_and_shows_remaining_count():
    items = [
        {
            "title": f"Item {i}",
            "url": f"https://example.com/{i}",
            "price": 100.0 + i,
            "reference_price": 100.0,
            "pct_above": float(i),
        }
        for i in range(8)
    ]
    message = w._build_digest_message("Test Retailer", items)
    assert message.count("• ") == w.DIGEST_TOP_N
    assert "+3 more" in message


def test_digest_signature_stable_regardless_of_order():
    items_a = [
        {"url": "https://example.com/1", "price": 10.0},
        {"url": "https://example.com/2", "price": 20.0},
    ]
    items_b = list(reversed(items_a))
    assert w._digest_signature(items_a) == w._digest_signature(items_b)


def test_digest_signature_changes_on_price_change():
    items_a = [{"url": "https://example.com/1", "price": 10.0}]
    items_b = [{"url": "https://example.com/1", "price": 12.0}]
    assert w._digest_signature(items_a) != w._digest_signature(items_b)


# ---- classify_deal() -------------------------------------------------------
def test_classify_deal_lazada_self_match_is_skipped():
    # Regression: Lazada IS the reference-price source, so its own listings
    # trivially equal their own catalog price (pct_above == 0) on every
    # run - flooded 28 individual "deal" alerts on the first real run
    # before this was fixed.
    assert w.classify_deal("lazada", 0.0) == "skip"


def test_classify_deal_lazada_requires_strict_discount():
    assert w.classify_deal("lazada", -5.0) == "deal"
    assert w.classify_deal("lazada", 5.0) == "above"


def test_classify_deal_other_retailers_allow_exact_msrp_match():
    # An independent seller landing exactly at MSRP is a genuine, meaningful
    # coincidence worth flagging - unlike Lazada checking itself.
    assert w.classify_deal("toysrus", 0.0) == "deal"
    assert w.classify_deal("dxcollection", -5.0) == "deal"
    assert w.classify_deal("carousell", 5.0) == "above"


# ---- Simplified Chinese support -----------------------------------------
CN_CATALOG = [
    {"title": "30th Celebration", "category": "Booster box", "price": 113.0},
    {"title": "30th Anniversary", "category": "Booster box", "price": 113.0},
]


def test_detect_language_chinese():
    assert w.detect_language("Pokemon 30th Celebration Chinese Booster Box") == "chinese"
    assert w.detect_language("Pokemon 30th Anniversary Simplified Chinese Booster Box sealed") == "chinese"
    assert w.detect_language("Pokemon CN 30th Celebration booster box") == "chinese"
    assert w.detect_language("宝可梦 简体中文 30th Pokemon booster box") == "chinese"
    # "cn" must be a whole word, not part of another word
    assert w.detect_language("Pokemon Scarlet Violet Booster Box (acne free)") == "english"
    # Japanese titles stay Japanese
    assert w.detect_language("Pokemon 30th Celebration Japanese Booster Box") == "japanese"


def test_chinese_listing_matches_resale_list():
    raw = {
        "title": "Pokemon 30th Anniversary Celebration Chinese Booster Box",
        "price": 80.0,
        "url": "https://www.carousell.sg/p/x-123/?t-id=abc",
        "in_stock": True,
        "target_name": "Carousell - Chinese booster box search",
    }
    item = w.process_listing(raw, {"english": [], "japanese": [], "chinese": CN_CATALOG})
    assert item is not None
    assert item["language"] == "chinese"
    assert item["reference_price"] == 113.0
    assert item["pct_above"] == -29.2


def test_chinese_listing_not_matched_against_japanese_catalog():
    raw = {
        "title": "Pokemon Abyss Eye Chinese Booster Box",
        "price": 30.0,
        "url": "https://example.com/c",
        "in_stock": True,
        "target_name": "Test Retailer",
    }
    # Abyss Eye is only in the Japanese catalog, so a Chinese listing has no match
    assert w.process_listing(raw, {"english": [], "japanese": JP_CATALOG, "chinese": CN_CATALOG}) is None


def test_classify_deal_chinese_uses_resale_threshold():
    # 80/113 = 71% of resale: a deal at the default 80% threshold
    assert w.classify_deal("carousell", -29.2, "chinese", 80) == "deal"
    # exactly 80% of resale: still a deal
    assert w.classify_deal("carousell", -20.0, "chinese", 80) == "deal"
    # 90% of resale: cheaper than resale but no room to flip -> digest
    assert w.classify_deal("carousell", -10.0, "chinese", 80) == "above"
    # existing behaviour for other languages is unchanged
    assert w.classify_deal("carousell", -10.0, "japanese", 80) == "deal"


def test_chinese_alert_message_shows_pct_of_resale():
    item = {
        "title": "Pokemon 30th Celebration Chinese Booster Box",
        "category": "Booster box",
        "language": "chinese",
        "price": 80.0,
        "reference_price": 113.0,
        "pct_above": -29.2,
        "url": "https://www.carousell.sg/p/x-123/",
        "retailer": "Carousell",
    }
    msg = w._build_individual_message(item)
    assert "Below resale" in msg
    assert "71% of SGD 113.00 resale" in msg
    assert "Simplified Chinese" in msg


def test_digest_signs_negative_pct_and_labels_resale():
    item = {
        "title": "Pokemon 30th Celebration Chinese Booster Box",
        "category": "Booster box",
        "language": "chinese",
        "price": 100.0,
        "reference_price": 113.0,
        "pct_above": -11.5,
        "url": "https://example.com/d",
        "retailer": "Carousell",
    }
    msg = w._build_digest_message("Carousell", [item])
    assert "resale SGD 113.00, -11.5%" in msg
    assert "+-" not in msg


def test_load_chinese_prices_reads_repo_file():
    catalog, deal_pct = w.load_chinese_prices()
    assert deal_pct == 80
    assert any(c["title"] == "30th Celebration" and c["price"] == 85 for c in catalog)


def test_load_chinese_prices_missing_file_is_harmless(tmp_path, monkeypatch):
    monkeypatch.setattr(w, "CHINESE_PRICES_FILE", tmp_path / "nope.json")
    assert w.load_chinese_prices() == ([], 80.0)


# ---- catalog refresh resilience --------------------------------------------
def test_empty_scrape_keeps_cached_catalog(tmp_path, monkeypatch):
    # Regression (Nov 2026): Lazada returned nothing from a GitHub runner and
    # the good cached English catalog was overwritten with an empty list.
    import json
    cache_file = tmp_path / "catalog_cache.json"
    cached = [{"title": "Chaos Rising", "category": "Elite Trainer Box", "price": 109.9}]
    cache_file.write_text(json.dumps({"english": {"fetched_at": 0, "catalog": cached}}))
    monkeypatch.setattr(w, "CATALOG_CACHE_FILE", cache_file)
    monkeypatch.setattr(w, "fetch_english_catalog", lambda: [])
    assert w.get_catalog("english") == cached
    assert json.loads(cache_file.read_text())["english"]["catalog"] == cached


# ---- buyout-lot alerts (Mac / local mode) -----------------------------------
def test_is_buyout_lot_keeps_real_lots():
    # Real titles from Carousell, 5 Nov 2026
    assert w.is_buyout_lot("full pokemon binder", 130)
    assert w.is_buyout_lot("Vintage Pokemon Cards Lot", 100)
    assert w.is_buyout_lot("pokemon bulk card", 66)
    assert w.is_buyout_lot("Album Binder + Pokemon Cards", 25)
    assert w.is_buyout_lot("Pokemon umbreon espeon leafeon glaceon sylveon flareon vaporeon jolteon gx card lot", 260)
    assert w.is_buyout_lot("Selling my childhood Pokémon cards 300 cards", 80)


def test_is_buyout_lot_drops_accessories_wanted_ads_and_sealed():
    assert not w.is_buyout_lot("Pokémon Ring Card Binder | 12 Slots, 3x3(900 slots) TCG Collection Binder Pokemon", 25)
    assert not w.is_buyout_lot("Pokemon Gengar Doodle Art binder non ring 9 pocket toploader binder with zip", 35)
    assert not w.is_buyout_lot("WTB - English Pokemon Collection Bulk card", 1)
    assert not w.is_buyout_lot("BUYING ALL SEALED POKEMON PRODUCTS CARDS COLLECTIONS", 100)
    assert not w.is_buyout_lot("Pokemon Mew ex 151 Ultra Premium Collection PSA 8", 150)
    assert not w.is_buyout_lot("Pokemon 30th Celebration Chinese Booster Box lot", 85)


def test_is_buyout_lot_price_band():
    assert not w.is_buyout_lot("pokemon bulk card", 5)  # too cheap to be worth a trip
    assert not w.is_buyout_lot("pokemon whole collection", 2500)  # far over the buying cap
    assert not w.is_buyout_lot("pokemon whole collection", None)


def _fake_listing(lid, title="full pokemon binder", price=130):
    return {"title": title, "price": price, "url": f"https://www.carousell.sg/p/x-{lid}/?t-id=abc", "in_stock": True}


def test_buyout_first_run_records_without_alerting(monkeypatch, tmp_path):
    monkeypatch.setattr(w, "STATE_FILE", tmp_path / "s.json")
    sent = []
    monkeypatch.setattr(w, "send_telegram", sent.append)
    monkeypatch.setattr(w, "BUYOUT_SEARCHES", [("pokemon binder", "u1")])
    state = {"seen": {}, "digests": {}}
    w.run_buyout_searches(state, fetch=lambda url: [_fake_listing(1000001), _fake_listing(1000002)])
    assert sent == []
    assert set(state["lots"]["pokemon binder"]) == {"1000001", "1000002"}

    # Next run: only the newly posted listing alerts, once.
    w.run_buyout_searches(state, fetch=lambda url: [_fake_listing(1000003), _fake_listing(1000001)])
    assert len(sent) == 1 and "x-1000003" in sent[0]
    w.run_buyout_searches(state, fetch=lambda url: [_fake_listing(1000003)])
    assert len(sent) == 1


def test_buyout_same_listing_in_two_searches_alerts_once(monkeypatch, tmp_path):
    monkeypatch.setattr(w, "STATE_FILE", tmp_path / "s.json")
    sent = []
    monkeypatch.setattr(w, "send_telegram", sent.append)
    monkeypatch.setattr(w, "BUYOUT_SEARCHES", [("a", "u1"), ("b", "u2")])
    state = {"seen": {}, "digests": {}, "lots": {"a": [], "b": []}}
    w.run_buyout_searches(state, fetch=lambda url: [_fake_listing(2000001)])
    assert len(sent) == 1


def test_buyout_failed_send_is_retried(monkeypatch, tmp_path):
    monkeypatch.setattr(w, "STATE_FILE", tmp_path / "s.json")
    monkeypatch.setattr(w, "BUYOUT_SEARCHES", [("a", "u1")])

    def boom(msg):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(w, "send_telegram", boom)
    state = {"seen": {}, "digests": {}, "lots": {"a": []}}
    w.run_buyout_searches(state, fetch=lambda url: [_fake_listing(3000001)])
    assert "3000001" not in state["lots"]["a"]


def test_targets_split_between_cloud_and_mac():
    local = [t.name for t in w.TARGETS if t.runs_on == "local"]
    assert local and all("Carousell" in n for n in local)
    assert all(t.runs_on == "cloud" for t in w.TARGETS if "Carousell" not in t.name)


def test_lot_message_escapes_and_strips_tracking():
    msg = w._build_lot_message(_fake_listing(4000001, title="pokemon <binder> & lot"), "pokemon binder")
    assert "&lt;binder&gt; &amp; lot" in msg
    assert "t-id" not in msg


def test_is_buyout_lot_rejects_real_non_lots_from_search_results():
    # All real titles from the 5 Nov 2026 newest-first searches that a
    # looser filter let through.
    for title, price in [
        ("Pokemon 30th Celebrations Binder Collection", 90),  # sealed product
        ("Pokemon White Flare Binder Collection – English", 98),
        ("Pokemon 30th Anniversary Trading Card Binders", 20),  # empty binder
        ("Black Leather Pokemon Trading Card Binder", 59),
        ("We're Goomy! Zip Binder (Pokemon Centre)", 98),
        ("Pokemon Poliwrath 9/75 Rare Legendary Collection card", 50),  # single from a set
        ("Flareon ex RC28 & Sylveon ex RC32 / Radiant Collection / Pokemon Card", 350),
        ("Pokemon Personality Test Vintage Old Back card", 20),
        ("Gengar Old Maid Japanese Pokemon Card", 25),
        ("Pokémon 151 Pokemon card clearance quitting sales wts prismatic evolution terastal festival", 23),
        ("Pokemon 30th Celebrations Anniversary list Ultra premium collection", 250),
    ]:
        assert not w.is_buyout_lot(title, price), title


def test_is_buyout_lot_keeps_real_lots_from_search_results():
    assert w.is_buyout_lot("Bundle of random old pokemon cards, commons and commons", 40)
    assert w.is_buyout_lot("Pokemon card clearance", 22)
    assert w.is_buyout_lot("Pokemon Reverse Holo Lot (Mid-era) card", 88)
    assert w.is_buyout_lot("Pokemon Cards AR lot card", 50)


# ---- dealer price lists (caught live 5 Nov 2026) ----------------------------
def test_multi_set_price_list_is_not_matched():
    raw = {
        "title": "Pokemon Japanese Booster Box Shrink/Unshrink 30th Anniversary, Abyss Eye Ninja Spinner/ Mega Dream/ Inferno X/ Terestal Festival",
        "price": 85.0,
        "url": "https://www.carousell.sg/p/x-1/",
        "in_stock": True,
        "target_name": "Carousell",
    }
    assert w.process_listing(raw, CATALOGS) is None


def test_single_set_listing_still_matched():
    raw = {
        "title": "Pokemon TCG Abyss Eye Japanese Booster Box Shrinked",
        "price": 40.0,
        "url": "https://www.carousell.sg/p/x-2/",
        "in_stock": True,
        "target_name": "Carousell",
    }
    assert w.process_listing(raw, CATALOGS) is not None


def test_same_listing_in_two_searches_alerts_once(monkeypatch, tmp_path):
    monkeypatch.setattr(w, "STATE_FILE", tmp_path / "s.json")
    monkeypatch.setattr(w, "MODE", "local")
    monkeypatch.setattr(w, "load_chinese_prices", lambda: ([], 80.0))
    monkeypatch.setattr(w, "get_market_catalogs", lambda: ({"english": [], "japanese": JP_MARKET}, 1.3))
    listing = {
        "title": "Pokemon TCG Abyss Eye Japanese Booster Box Shrinked",
        "price": 70.0,
        "url": "https://www.carousell.sg/p/x-3/?t-id=1",
        "in_stock": True,
    }
    monkeypatch.setitem(w.PARSERS, "fake", lambda t: [dict(listing, target_name=t.name)])
    monkeypatch.setattr(w, "TARGETS", [
        w.Target(name="Search A", url="a", parser="fake", runs_on="local"),
        w.Target(name="Search B", url="b", parser="fake", runs_on="local"),
    ])
    sent = []
    monkeypatch.setattr(w, "send_telegram", sent.append)
    w._run_all_targets({"seen": {}, "digests": {}})
    assert len(sent) == 1


# ---- resale pricing (TCGplayer market) --------------------------------------
JP_MARKET = [
    {"title": "Abyss Eye Booster Box", "category": "Booster box", "usd": 79.15},
    {"title": "Mega Dream ex Booster Box", "category": "Booster box", "usd": 86.18},
    {"title": "MEGA Expansion 30th Celebration Booster Box", "category": "Booster box", "usd": 204.15},
]
EN_MARKET = [
    {"title": "Scarlet & Violet Elite Trainer Box [Koraidon]", "category": "Elite Trainer Box", "usd": 60.0},
    {"title": "Scarlet & Violet 151 Elite Trainer Box", "category": "Elite Trainer Box", "usd": 120.0},
    {"title": "Scarlet & Violet 151 Pokemon Center Elite Trainer Box", "category": "Elite Trainer Box", "usd": 400.0},
    {"title": "Crown Zenith Elite Trainer Box", "category": "Elite Trainer Box", "usd": 309.10},
    {"title": "Crown Zenith Pokemon Center Elite Trainer Box Plus", "category": "Elite Trainer Box", "usd": 534.82},
    {"title": "Mega Evolution: Perfect Order Elite Trainer Box", "category": "Elite Trainer Box", "usd": 60.0},
    {"title": "Mega Evolution: Perfect Order Pokemon Center Elite Trainer Box", "category": "Elite Trainer Box", "usd": 95.0},
    {"title": "30th Celebration Pokemon Center Elite Trainer Box", "category": "Elite Trainer Box", "usd": 180.0},
    {"title": "30th Celebration Elite Trainer Box", "category": "Elite Trainer Box", "usd": 90.0},
]
MARKET = {"english": EN_MARKET, "japanese": JP_MARKET}


def _raw(title, price, url="https://www.carousell.sg/p/x-9/"):
    return {"title": title, "price": price, "url": url, "in_stock": True, "target_name": "Carousell"}


def test_sealed_products_from_group_keeps_only_single_sealed_items():
    products = [
        {"productId": 1, "name": "Crown Zenith Elite Trainer Box", "extendedData": []},
        {"productId": 2, "name": "Crown Zenith Elite Trainer Box Case", "extendedData": []},
        {"productId": 3, "name": "Code Card - Crown Zenith Elite Trainer Box", "extendedData": []},
        {"productId": 4, "name": "Pikachu", "extendedData": [{"name": "Number", "value": "160/159"}]},
        {"productId": 5, "name": "Crown Zenith Mini Tin Display", "extendedData": []},
        {"productId": 6, "name": "Crown Zenith Booster Bundle", "extendedData": []},
        {"productId": 7, "name": "Crown Zenith Collection [Regieleki V]", "extendedData": []},
    ]
    prices = [{"productId": i, "marketPrice": 10.0 * i} for i in range(1, 8)]
    prices.append({"productId": 1, "marketPrice": None})
    out = w.sealed_products_from_group(products, prices)
    assert [(e["title"], e["category"], e["usd"]) for e in out] == [
        ("Crown Zenith Elite Trainer Box", "Elite Trainer Box", 10.0),
        ("Crown Zenith Booster Bundle", "Booster bundle", 60.0),
    ]


def test_market_match_prefers_most_specific_set():
    m = w.find_market_match("Pokemon Scarlet & Violet 151 Elite Trainer Box", "Elite Trainer Box", EN_MARKET)
    assert m["title"] == "Scarlet & Violet 151 Elite Trainer Box"


def test_market_match_respects_pokemon_center_and_plus_editions():
    m = w.find_market_match("Pokemon TCG Perfect Order Elite Trainer Box", "Elite Trainer Box", EN_MARKET)
    assert m["usd"] == 60.0
    m = w.find_market_match("Pokemon Centre Perfect Order ETB", "Elite Trainer Box", EN_MARKET)
    assert m["usd"] == 95.0
    m = w.find_market_match("Pokemon Crown Zenith Pokemon Center ETB Plus", "Elite Trainer Box", EN_MARKET)
    assert m["usd"] == 534.82
    m = w.find_market_match("Pokémon Crown Zenith Elite Trainer Box (ETB)", "Elite Trainer Box", EN_MARKET)
    assert m["usd"] == 309.10


def test_market_match_short_titles():
    # Real titles from Carousell, 5 Nov 2026, that the old matcher missed
    m = w.find_market_match("Pokémon Center 30th Elite Trainer Box (ETB)", "Elite Trainer Box", EN_MARKET)
    assert m["title"] == "30th Celebration Pokemon Center Elite Trainer Box"
    m = w.find_market_match("Pokemon 151 ETB", "Elite Trainer Box", EN_MARKET)
    assert m["title"] == "Scarlet & Violet 151 Elite Trainer Box"
    # A title that names no product matches nothing
    assert w.find_market_match("Pokemon Elite Trainer Box ETB instock", "Elite Trainer Box", EN_MARKET) is None


def test_market_match_japanese_high_class_set():
    m = w.find_market_match("Pokemon Japanese M2a Mega Dream ex Booster Box", "Booster box", JP_MARKET)
    assert m["usd"] == 86.18


def test_evaluate_listing_verdicts():
    # 30th PC ETB market US$180 x 1.3 = S$234
    deal = w.evaluate_listing(_raw("Pokémon Center 30th Celebration Elite Trainer Box", 150), MARKET, [], 1.3)
    assert deal["verdict"] == "deal" and deal["pct_of_market"] == 64
    fair = w.evaluate_listing(_raw("Pokémon Center 30th Celebration Elite Trainer Box", 220), MARKET, [], 1.3)
    assert fair["verdict"] == "none"
    check = w.evaluate_listing(_raw("Pokémon Center 30th Elite Trainer Box (ETB)", 80), MARKET, [], 1.3)
    assert check["verdict"] == "check"  # S$80 = 34% of market: mismatch, variant or scam
    jp = w.evaluate_listing(_raw("Pokemon Japanese Abyss Eye Booster Box", 87), MARKET, [], 1.3)
    assert jp["verdict"] == "none" and jp["pct_of_market"] == 85


def test_evaluate_listing_chinese_uses_resale_list():
    cn = [{"title": "30th Celebration", "category": "Booster box", "price": 85.0}]
    item = w.evaluate_listing(_raw("Pokemon 30th Celebration Simplified Chinese Booster Box", 65), MARKET, cn, 1.3)
    assert item["verdict"] == "deal" and item["source"] == "local resale list"
    item = w.evaluate_listing(_raw("Pokemon 30th Celebration Simplified Chinese Booster Box", 85), MARKET, cn, 1.3)
    assert item["verdict"] == "none"


def test_evaluate_listing_skips_unknown_and_multi_set():
    assert w.evaluate_listing(_raw("Pokemon Gem Vol 5 Booster Box", 42), MARKET, [], 1.3) is None
    assert w.evaluate_listing(_raw(
        "Pokemon Japanese Booster Box Abyss Eye / Ninja Spinner / Mega Dream / Inferno X", 85), MARKET, [], 1.3) is None


def test_resale_message_shapes():
    item = w.evaluate_listing(_raw("Pokémon Center 30th Celebration Elite Trainer Box <new>", 150), MARKET, [], 1.3)
    msg = w._build_resale_message(item)
    assert "Deal:" in msg and "64% of market S$234.00" in msg and "&lt;new&gt;" in msg
    assert "Official store" in w._build_resale_message(item, official=True)
    item["verdict"] = "check"
    assert "Check:" in w._build_resale_message(item)


def test_shop_restock_alerts_again_after_selling_out(monkeypatch, tmp_path):
    monkeypatch.setattr(w, "STATE_FILE", tmp_path / "s.json")
    monkeypatch.setattr(w, "MODE", "cloud")
    monkeypatch.setattr(w, "load_chinese_prices", lambda: ([], 80.0))
    monkeypatch.setattr(w, "get_market_catalogs", lambda: (MARKET, 1.3))
    listing = _raw("Pokémon Center Trading Card Game: 30th Celebration Pokémon Center Elite Trainer Box", 109.9,
                   url="https://www.lazada.sg/products/pdp-i1.html")
    stock = {"items": [listing]}
    monkeypatch.setitem(w.PARSERS, "lazada", lambda t: [dict(x, target_name=t.name) for x in stock["items"]])
    monkeypatch.setattr(w, "TARGETS", [w.Target(name="Official", url="u", parser="lazada")])
    sent = []
    monkeypatch.setattr(w, "send_telegram", sent.append)
    state = {"seen": {}, "digests": {}}
    w._run_all_targets(state)  # in stock at retail = 47% of market: a hot restock
    w._run_all_targets(state)  # still in stock: no repeat
    stock["items"] = []
    w._run_all_targets(state)  # sold out
    stock["items"] = [listing]
    w._run_all_targets(state)  # restocked: alerts again
    assert len(sent) == 2
