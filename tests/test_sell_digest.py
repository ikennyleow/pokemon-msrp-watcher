"""Unit tests for the daily sell-side price check (no network)."""

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import sell_digest as sd  # noqa: E402

HOLDINGS = [
    {"id": "1", "name": "Crown Zenith", "setName": "Crown Zenith", "category": "Elite Trainer Box", "quantity": 1, "currentPrice": 300.0},
    {"id": "2", "name": "Crown Zenith", "setName": "Crown Zenith", "category": "Elite Trainer Box", "quantity": 2, "currentPrice": 300.0},
    {"id": "3", "name": "151", "setName": "151", "category": "Elite Trainer Box", "quantity": 2, "currentPrice": 100.0, "neverSell": True},
    {"id": "4", "name": "Surging Sparks", "setName": "Surging Sparks", "category": "Booster Bundle", "quantity": 0, "currentPrice": 40.0},
    {"id": "5", "name": "Terastal Festival", "setName": "Terastal Festival", "variant": "Japanese", "category": "Booster Box", "quantity": 1, "currentPrice": 150.0},
    {"id": "6", "name": "Mythical Squishy", "setName": "Mythical Squishy", "variant": "Premium Collection", "category": "Other", "quantity": 1, "currentPrice": 270.0},
    {"id": "7", "name": "Stellar Crown", "setName": "Stellar Crown", "category": "Half Booster Box", "quantity": 1, "currentPrice": 80.0},
]


def _products():
    return {p["title"]: p for p in sd.holding_products(HOLDINGS, 1.3)}


def test_holding_products_groups_and_filters():
    products = _products()
    assert set(products) == {
        "Crown Zenith Elite Trainer Box",
        "Terastal Festival Japanese Booster Box",
        "Mythical Squishy Premium Collection",
    }
    cz = products["Crown Zenith Elite Trainer Box"]
    assert cz["quantity"] == 3 and cz["market_sgd"] == 390.0 and cz["category"] == "Elite Trainer Box"
    assert products["Terastal Festival Japanese Booster Box"]["language"] == "japanese"
    assert products["Mythical Squishy Premium Collection"]["category"] == "Premium/Special collection"


def test_match_product_recognises_listings():
    products = list(_products().values())
    assert sd.match_product("Pokémon Crown Zenith Elite Trainer Box (ETB) English", products)["title"] == "Crown Zenith Elite Trainer Box"
    assert sd.match_product("Pokemon Japanese Terastal Fest ex Booster Box SV8a", products)["title"] == "Terastal Festival Japanese Booster Box"
    # English listing never matches the Japanese holding, and vice versa
    assert sd.match_product("Pokemon Terastal Festival Booster Box", products) is None
    # Pokemon Center edition is a different product
    assert sd.match_product("Pokemon Crown Zenith Pokemon Center ETB", products) is None
    # empty boxes etc. don't count as a competing ask
    assert sd.match_product("Pokemon Crown Zenith ETB empty box", products) is None


def test_advise():
    p = {"market_sgd": 390.0}
    assert sd.advise(p, 420, 360)[0] == "cut"
    assert sd.advise(p, 365, 360)[0] == "ok"
    assert sd.advise(p, 300, 360) == ("raise", "cheapest by far. Room to raise to ~S$359")
    assert sd.advise(p, None, 360) == ("list", "not listed. List at ~S$359")
    assert sd.advise(p, None, None) == ("list", "not listed, no other sellers. List at ~S$390")
    assert sd.advise(p, 370, None)[0] == "ok"


def test_build_digest_sections():
    products = _products()
    rows = [
        {"product": products["Crown Zenith Elite Trainer Box"], "my_ask": None, "lowest_other": 360.0, "n_other": 4,
         "action": "list", "text": "not listed. List at ~S$359"},
        {"product": products["Terastal Festival Japanese Booster Box"], "my_ask": 250.0, "lowest_other": 200.0, "n_other": 2,
         "action": "cut", "text": "undercut. Cut to ~S$199 or hold"},
        {"product": products["Mythical Squishy Premium Collection"], "my_ask": 400.0, "lowest_other": 395.0, "n_other": 1,
         "action": "ok", "text": "priced right"},
    ]
    msg = sd.build_digest(rows, dt.date(2026, 11, 6))
    assert "Fri 06 Nov" in msg
    assert "<b>Your listings</b> (2)" in msg
    assert "Terastal Festival Japanese Booster Box: you S$250 · lowest other S$200 (2)" in msg
    assert "1 priced in line with Carousell" in msg and "Mythical" not in msg
    assert "<b>Worth listing</b> (top 1 of 1 not listed)" in msg
    assert "• Crown Zenith Elite Trainer Box ×3: lowest S$360 (4) · market S$390" in msg


def test_build_digest_caps_unlisted_to_most_valuable():
    rows = [
        {"product": {"title": f"P{i}", "quantity": 1, "market_sgd": float(i)}, "my_ask": None,
         "lowest_other": None, "n_other": 0, "action": "list", "text": ""}
        for i in range(1, 12)
    ]
    msg = sd.build_digest(rows, dt.date(2026, 11, 6))
    assert "(top 5 of 11 not listed)" in msg
    assert "P11" in msg and "P7" in msg and "P6 " not in msg
    assert "None of your holdings are listed." in msg


def test_digest_due_once_a_day_after_9am():
    assert not sd.digest_due({}, dt.datetime(2026, 11, 6, 8, 59))
    assert sd.digest_due({}, dt.datetime(2026, 11, 6, 9, 0))
    assert not sd.digest_due({"sell_digest_date": "2026-11-06"}, dt.datetime(2026, 11, 6, 15, 0))
    assert sd.digest_due({"sell_digest_date": "2026-11-05"}, dt.datetime(2026, 11, 6, 9, 30))


def test_parse_cards_skips_sold_and_reserved():
    page = """
    <a href="/p/a-1/?t-id=x"><img alt="Pokemon Crown Zenith ETB"/>S$380</a>
    <a href="/p/b-2/"><img alt="Pokemon Crown Zenith ETB"/>Reserved S$350</a>
    <a href="/p/c-3/"><img alt="Pokemon Crown Zenith ETB"/>no price</a>
    """
    out = sd._parse_cards(page, "u")
    assert out == [{"title": "Pokemon Crown Zenith ETB", "price": 380.0, "url": "https://www.carousell.sg/p/a-1/"}]


def test_run_sell_digest_end_to_end(monkeypatch, tmp_path):
    monkeypatch.setattr(sd.w, "STATE_FILE", tmp_path / "s.json")
    monkeypatch.setattr(sd, "tracker_password", lambda: "pw")
    monkeypatch.setattr(sd, "fetch_holdings", lambda pw: HOLDINGS)
    monkeypatch.setattr(sd.w, "get_market_catalogs", lambda: ({}, 1.3))
    own = [{"title": "Pokémon Crown Zenith Elite Trainer Box (ETB)", "price": 420.0, "url": "https://www.carousell.sg/p/mine-1/"}]

    def fake_fetch(urls):
        return own, {u: own + [
            {"title": "Pokemon Crown Zenith ETB sealed", "price": 360.0, "url": "https://www.carousell.sg/p/x-2/"},
            {"title": "Pokemon Crown Zenith ETB case", "price": 2000.0, "url": "https://www.carousell.sg/p/x-3/"},
            # 2022 listing at release price, never taken down
            {"title": "Pokemon Crown Zenith ETB", "price": 250.0, "url": "https://www.carousell.sg/p/old-1210710902/"},
            {"title": "Pokemon Crown Zenith ETB", "price": 150.0, "url": "https://www.carousell.sg/p/new-1473117544/"},
        ] for u in urls}

    monkeypatch.setattr(sd, "fetch_carousell_pages", fake_fetch)
    sent = []
    monkeypatch.setattr(sd.w, "send_telegram", sent.append)
    state = {}
    sd.run_sell_digest(state, now=dt.datetime(2026, 11, 6, 9, 5))
    assert len(sent) == 1
    assert "you S$420 · lowest other S$360 (1)" in sent[0]  # own listing and the case excluded
    assert state["sell_digest_date"] == "2026-11-06"
    sd.run_sell_digest(state, now=dt.datetime(2026, 11, 6, 10, 0))
    assert len(sent) == 1  # once a day


def test_listings_must_be_the_same_tcgplayer_product_as_the_holding():
    # Real-data regression: a base "Scarlet & Violet ETB" holding matched
    # Destined Rivals / Paldean Fates ETB listings ("Scarlet & Violet ..." titles).
    market = {"english": [
        {"title": "Scarlet & Violet Elite Trainer Box [Koraidon]", "category": "Elite Trainer Box", "usd": 60.0},
        {"title": "Destined Rivals Elite Trainer Box", "category": "Elite Trainer Box", "usd": 110.0},
        {"title": "Paldean Fates Elite Trainer Box", "category": "Elite Trainer Box", "usd": 430.0},
    ], "japanese": []}
    products = sd.holding_products([
        {"name": "Scarlet & Violet", "category": "Elite Trainer Box", "quantity": 2, "currentPrice": 140.0},
    ], 1.3)
    sd.attach_market_entries(products, market)
    assert products[0]["_entry"]["title"] == "Scarlet & Violet Elite Trainer Box [Koraidon]"
    assert sd.match_product("Pokémon Scarlet & Violet Destined Rivals Elite Trainer Box (ETB)", products, market) is None
    assert sd.match_product("Pokémon Scarlet & Violet Paldean Fates Elite Trainer Box (ETB)", products, market) is None
    assert sd.match_product("Pokemon Scarlet & Violet Base Set ETB Koraidon", products, market) is products[0]


def test_super_premium_collection_category_is_sellable():
    products = sd.holding_products([
        {"name": "Charizard ex", "category": "Super-Premium Collection", "quantity": 1, "currentPrice": 135.74},
    ], 1.3)
    assert products[0]["title"] == "Charizard ex Super-Premium Collection"
    assert products[0]["category"] == "Premium/Special collection"


def test_stale_listing_ids():
    newest = 1473117544
    assert sd.is_stale("https://www.carousell.sg/p/silver-tempest-booster-box-1210710902/", newest)
    assert not sd.is_stale("https://www.carousell.sg/p/silver-tempest-booster-box-1460000000/", newest)
    assert not sd.is_stale("https://www.carousell.sg/p/x-2/", newest)  # no numeric id: keep
