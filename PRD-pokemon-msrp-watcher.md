# PRD: Pokemon TCG MSRP Watcher (Singapore) — v2

## 1. Summary
A monitoring service that watches a set of Singapore retailer product pages for sealed Pokémon TCG product, and sends a Telegram message whenever a listing is found **in stock at or below its live reference price on Pokemon Center's official Singapore storefront** (the "Pokemon Store Online Singapore" store on Lazada). Scope is any sealed product (booster packs, booster bundles, elite trainer boxes, booster boxes, tins, premium/special collections) — not limited to a specific set or SKU.

Unlike v1, MSRP is no longer a static per-category number maintained by hand. It's looked up live, per product, against Pokemon Center's own current SGD pricing — which is the actual, ground-truth "official" price, and stays correct automatically as new sets release or prices change, with no manual table upkeep.

Listings priced above their matched reference are not ignored outright, but don't get an individual real-time alert either: they're batched into one per-retailer digest message showing the closest-to-MSRP listings and how far above reference each one is, re-sent only when that digest's contents actually change (§8.9-8.10). Listings with no confident match to anything in the reference catalog are ignored entirely (no reference price = can't verify MSRP at all, nothing useful to report).

## 2. Problem
Sealed Pokémon product in Singapore mostly sells above true MSRP once it reaches secondary/local card shops (checked live in v1: DXCOLLECTION, Cards Citadel, and similar run 1.5–3x list price). Genuine MSRP stock only surfaces at a small number of official/big-box retail channels, and it sells out within minutes of restocking. Manually refreshing these pages is not viable; the user wants a background watcher that reacts fast enough to catch a restock window.

A static reference-price table (v1's approach) needs manual maintenance and can drift out of date. Pokemon Center's own SG storefront is the actual source of truth for official pricing — comparing against it directly is both more accurate and self-maintaining.

## 3. Goals
- Detect new in-stock sealed Pokémon TCG listings across target retailers.
- For each listing, determine its official reference price by fuzzy-matching it against the appropriate live reference catalog (§7).
- Send an **individual** Telegram alert, immediately, for each listing priced at or below its matched reference price — product name, price, retailer, reference price, and a direct link.
- Send a **per-retailer digest** Telegram message summarizing listings priced above their matched reference — closest-to-MSRP items first, with % above MSRP shown per item — re-sent only when that digest's contents change, not on every poll (§8.9-8.10).
- Ignore entirely (no tracking, no notification of any kind): listings with no confident catalog match.
- Avoid duplicate notifications for stock that is still sitting there, unchanged in price, from a previous check — applies to both individual alerts and digests.
- Run unattended on a recurring schedule.

## 4. Non-goals
- Not scoped to a specific Pokémon TCG set or product line — any sealed product counts.
- **Auto-add-to-cart / cart reservation is out of scope for this version** — deferred, see §13. Every notification is a plain link to the listing; the user always completes checkout themselves via the retailer's own page. No cart automation, no saved login sessions, no payment method ever stored or entered by the service.
- Not maintaining a hand-written MSRP table — the Pokemon Center SG storefront catalog is the only reference source. If a product isn't sold there, it's simply not verifiable and is skipped (§7).
- Not guaranteeing coverage of every SG retailer — starts with a curated, confirmed target list (§6) and can be extended later.
- Not doing currency conversion — both the reference source (Pokemon Center SG storefront) and all checked retailers price in SGD already.

## 5. Users
Single user (Kenny), personal use, notified via a personal Telegram chat.

## 6. Target retailers
All retailers are checked on the same shared polling schedule (§9) — there is no frequency tiering in this version. Lazada plays a dual role: it's both the reference-price source (§7) and a normal checked retailer (its own listings can still be flagged if temporarily discounted below its own regular price).

| Retailer | URL | Role |
|---|---|---|
| Pokemon Store Online Singapore (Lazada, official) | https://www.lazada.sg/pokemon-store-online-singapore/?q=All-Products&from=wangpu&langFlag=en&pageTypeId=2 | Reference-price source AND checked retailer. JS-rendered — needs a headless-browser fetch (confirmed in prior build: Playwright, not plain HTTP). |
| Toys"R"Us Singapore | https://www.toysrus.com.sg/pokemon/ | Checked retailer. Server-rendered HTML (Salesforce Commerce Cloud) — plain HTTP GET + parse confirmed working in prior build. |
| Amazon.sg | https://www.amazon.sg/elite-trainer-box/s?k=elite+trainer+box (+ similar per product type) | Checked retailer, best-effort. Scraping is against Amazon's ToS — keep at low priority, expect blocking. |
| DXCOLLECTION — Booster Boxes | https://dxcollection.com.sg/product-category/pokemon-cards-singapore/pokemon-booster-boxes/ | Checked retailer. Confirmed WooCommerce site. |
| DXCOLLECTION — Elite Trainer Boxes | https://dxcollection.com.sg/product-category/pokemon-cards-singapore/pokemon-elite-trainer-boxes-etb/ | Checked retailer. Same site/parser as above. |
| Carousell (search: "pokemon booster box") | https://www.carousell.sg/search/pokemon%20booster%20box | Checked retailer. JS-rendered + bot mitigation — needs headless-browser fetch. Noisiest source (mixes in non-sealed/non-Pokemon results); relies on the category filter (§7) more heavily than the others. |

**Explicitly not included** (no confirmed listing page found in prior research): Popular/Challenger online, TikTok Shop official storefronts (would need a paid third-party monitoring API), other local/secondary card shops beyond DXCOLLECTION. Any of these can be added later given a confirmed product-category URL.

### 6.1 Where each target runs (added Nov 2026)
Carousell blocks cloud servers (0 listings from GitHub Actions since Nov 2026, while the same searches work from a home connection). Carousell targets are therefore marked `runs_on="local"` and run on Kenny's Mac every 30 minutes via launchd (`mac/`, `WATCHER_MODE=local`); every other target runs in the GitHub Action. The Mac copy pulls the repo before each run and only reads the catalogs the cloud run commits; it never writes them, and keeps its own gitignored `state_local.json`.

The Mac copy also sends **buyout-lot alerts**: one Telegram message per newly posted collection, binder or bulk lot (S$20–400) from five newest-first Carousell searches. These can't be priced automatically, so the alert is about speed. The title filter (`is_buyout_lot`) was tuned against 103 real listings, since most search results are empty binders, sealed "Binder Collection" products, single cards from sets named "... Collection", or dealers' keyword-stuffed titles. A search's first run only records existing listings, so there's no flood of old listings.

### 6.2 Target changes (Nov 2026)
- Removed Toys"R"Us (its /pokemon/ page is toys and figures; it sells no Pokémon cards online) and Amazon.sg (0 listings on every run).
- Added newest-first Carousell searches (Mac) for Elite Trainer Boxes, booster bundles and premium collections, alongside booster boxes and Chinese booster boxes.
- The official store page intermittently renders empty (2 of 4 runs on 5 Nov), so it's retried up to 3 times with longer waits and reported as an error if still empty.

## 7. MSRP determination
There are **two** reference catalogs, because English/International and Japanese Pokémon TCG product are genuinely different product lines with different official distributors and no shared official Singapore price to compare against directly.

### 7.1 English/International reference catalog
The Pokemon Store Online Singapore Lazada storefront (§6) is scraped to build a `{title, price_sgd}` catalog for every product currently listed there. Refreshed every 4 hours (proposed), cached between refreshes so the main poll loop doesn't re-scrape Lazada on every check.

### 7.2 Japanese reference catalog
[pokemon-card.com/products/](https://www.pokemon-card.com/products/) — the official Pokémon Card Game Japan site, run directly by The Pokémon Company/Creatures Inc. — publishes 希望小売価格 (official suggested retail price) in JPY for every release, confirmed live during PRD research (e.g. "アビスアイ" (Abyss Eye) ¥200, "ブラックボルト" (Black Bolt) ¥290). Two things this catalog needs beyond a straight scrape:

- **Box vs. pack pricing**: published prices are per single pack (拡張パック), not per box. Japanese booster boxes are standard 30 packs/box, so box reference price = pack price × 30. Treated as a documented assumption, to be spot-checked against a couple of real box listings once built (some special/deluxe releases may package differently).
- **Currency conversion**: prices are JPY, all target-retailer prices are SGD. Needs a live JPY→SGD rate, refreshed on a similarly slow cadence (proposed: daily — exchange rates don't need to be fresher than that for this purpose) from a free, no-API-key source (e.g. Frankfurter.dev, ECB-rate-based).
- **Set-name mapping**: retailer listings use English-romanized set names ("Abyss Eye"), the official source uses katakana ("アビスアイ"). These correspond closely but aren't a literal string match. Given a manageable, finite number of active sets (~10-20 at a time), maintain a small lookup table pairing each set's Japanese name to its English name, refreshed occasionally as new sets release — same spirit as the catalog refresh itself, not a one-time hardcode.

Refreshed on the same cadence as §7.1 (every 4 hours), cached the same way.

### 7.3 Language detection
Every listing is tagged English or Japanese based on its title (keywords like "japanese", "jap", "jp" indicate Japanese import; absence of these indicates English/International — DXCOLLECTION and Carousell titles reliably self-label this already, per titles observed during research like "Pokemon TCG – Abyss Eye **Japanese** Booster Box"). This determines which catalog (§7.1 or §7.2) the listing is matched against in §7.5.

### 7.4 Classification (first-pass filter)
Every scraped listing (from any retailer, including Lazada itself) is first classified by keyword-matching its title against known sealed-product types, same categories as v1:

| Category | Title keywords (any match) |
|---|---|
| Elite Trainer Box (ETB) | "elite trainer box", "etb" |
| Booster bundle | "booster bundle" |
| Booster box | "booster box" |
| Premium / Special collection | "premium collection", "special collection", "collection box" |
| Tin | "tin" |
| Single booster pack | "booster pack", "booster" |

Keyword checks run most-specific-first (e.g. "booster bundle" before generic "booster"), match on whole words/phrases (not raw substrings — "tin" must not match inside "destiny"), and allow simple plurals ("booster boxes" also matches "booster box"). A listing that doesn't match any category, or doesn't mention "pokemon"/"pokémon" in its title, is discarded immediately — it never reaches the matching step (§7.5).

This step exists purely as a filter (is this even a sealed Pokémon TCG product worth considering?) — it no longer determines a price the way it did in v1.

### 7.5 Matching against the reference catalog
For each classified listing, attempt to find its corresponding entry in the catalog selected by its language (§7.3 → §7.1 or §7.2):
1. The listing's category (§7.4) must match the reference catalog entry's category exactly (an ETB listing only matches against ETB entries in the catalog, never a Booster Box entry).
2. Within that category, fuzzy-match on set/product name (e.g. "Mega Evolution: Chaos Rising" ↔ "Chaos Rising"; for Japanese, via the set-name mapping in §7.2) to find the specific matching catalog entry.
3. If no catalog entry meets both conditions with reasonable confidence, the listing has no verifiable MSRP and is discarded entirely — not individually notified, not added to any digest, not written to state.
4. If a match is found, its price (converted to SGD per §7.2 if Japanese) becomes this listing's reference price for the comparison in §7.6.

(Exact similarity-threshold tuning for step 2 is an implementation detail to be refined against real catalog data — not a product decision.)

### 7.6 Price comparison
For every listing with both a category match and a confident catalog match, compute `pct_above = (listing_price - matched_reference_price) / matched_reference_price * 100`:
- If `listing_price <= matched_reference_price` (i.e. `pct_above <= 0`): eligible for an **individual** notification (§8.9).
- If `listing_price > matched_reference_price`: not individually notified — instead contributes one entry to that retailer's above-MSRP digest (§8.10). Note: since Japanese-import listings inherently carry some legitimate markup over pure JPY-converted price (shipping, duties, reseller margin), Japanese-sourced individual alerts are expected to be rarer than English ones — this is accepted as correct behavior (a rare genuine deal is still worth surfacing), not a bug to work around. Japanese listings still contribute to digests same as English ones.

### 7.7 Simplified Chinese reference list (added Nov 2026)
Simplified Chinese sealed product has no official Singapore retail price, so it is matched against a short hand-kept list of local **resale** prices in `chinese_prices.json` (one entry per name sellers use, e.g. "30th Celebration" and "30th Anniversary"). Language detection tags a listing Chinese when its title says "chinese", "simplified", "CN", "中文" or "简体" (checked before the Japanese markers). Because the reference is a resale price rather than MSRP, a Chinese listing gets an individual alert only at or below `deal_pct` (default 80%) of it, leaving room to resell; anything above that goes to the digest with the reference labelled "resale". A dedicated Carousell search for Chinese booster boxes (newest first) is included in the targets.

### 7.8 Resale pricing replaces MSRP as the alert yardstick (Nov 2026)
A review against 137 real Carousell listings (5 Nov 2026) found the MSRP yardstick missed most of what matters for a resale business: the English reference only covered the ~19 products the official store currently sells, so older/out-of-print sealed product (where most margin is) was invisible, and Japanese boxes always read as "+80-120% above MSRP". Listings are now priced against **TCGplayer market prices** (tcgcsv.com, all English and Japanese sealed products, refreshed daily, ~800 EN / ~150 JP), converted to SGD:
- **Deal**: at or below 75% of market and at least S$20 below it.
- **Check**: under 50% of market. Usually a mismatch, a variant's price or a scam; still worth a look. Not used for the official store, where retail far below market is a hot restock.
- **Official store**: alerts only after its product page confirms it's in stock (the store's grid shows sold-out items like available ones), and again after each restock.
- Matching requires Pokemon Center / Plus / Mini editions and edition numbers (30th, 25th, 151) to agree, uses TCGplayer set codes (M6a, SV8a) when a title has one, keeps variant names TCGplayer writes in brackets, folds common naming variants (Anniversary/Celebration, Festival/Fest, plurals, "ex"), and skips titles naming 2+ sets and non-sealed listings (graded, empty, opened, accessories).
- The JPY-MSRP catalog (§7.2) and above-MSRP digests (§8.10) are no longer used for alerts.

### 7.9 Daily sell-side price check (Nov 2026, Mac only)
Once a day (first local run after 09:00), `sell_digest.py` reads Kenny's tracker holdings (read-only, via the tracker's storage API and its password in the gitignored `secrets.json`) and his Carousell profile, searches Carousell for each sellable holding (quantity > 0, not Never Sell), and sends one Telegram message: cut/raise suggestions for his live listings (vs the lowest other ask and market value) and the five most valuable unlisted holdings. Holdings and listings are compared by identity: both must resolve to the same TCGplayer product. Competing asks outside 30–300% of market (cases, deposits, placeholders) are ignored. It never changes a listing or the tracker.

## 8. Functional requirements
1. **Refresh reference catalogs**: scrape the Lazada English catalog (§7.1) and the pokemon-card.com Japanese catalog (§7.2, including the JPY→SGD rate and box-price derivation), caching both, on the schedule in §7.1/§7.2.
2. **Fetch**: for each configured retailer target, retrieve current page content, on the shared polling schedule (§9). JS-rendered targets (Lazada, Carousell) need a headless-browser fetch, not plain HTTP GET.
3. **Parse**: extract each listing's title, price, stock status, and product URL. Parsing is retailer-specific and must be verified against live HTML per site.
4. **Detect language**: tag each listing English or Japanese (§7.3).
5. **Classify**: filter to known sealed-product categories (§7.4). Discard anything that doesn't match.
6. **Match & compare**: attempt to match each classified listing against the catalog selected by its language (§7.5) and compare price (§7.6). Discard (no tracking at all) anything with no confident match. Everything else — at/below reference or above it — proceeds to one of the two notification paths below.
7. **Filter (stock)**: keep only listings currently in stock.
8. **Dedupe (individual alerts)**: track previously-alerted at/below-reference matches (by retailer + product URL + price) in persistent state so an unchanged, still-in-stock listing doesn't re-trigger a notification on every poll. A price change on an already-seen listing is treated as a new candidate (re-run the full match/compare — if it's risen above its reference it now moves to the digest path instead; if still at/below, alert again with the updated price).
9. **Notify (individual)**: for each new eligible at/below-reference match, send a Telegram message immediately via the Bot API, containing: product title, category, language, retailer name, price, matched reference price, and a direct link to the listing. No cart link (§4) — just the listing.
10. **Build & notify (digest)**: for each retailer, collect all its current in-stock, classified, catalog-matched, above-reference listings this poll. Sort by `pct_above` ascending (closest to MSRP first), cap the itemized list to the top N (proposed N=5, §11) plus a total count of the rest, and include each shown item's price, matched reference price, and `pct_above`. Compare this digest's content (the specific set of listings + their prices) against the last-sent digest for that retailer (tracked in state, separately from individual-alert dedup) — only send via Telegram if it changed; otherwise skip silently this poll.
11. **Schedule**: run automatically and continuously without manual invocation, on the polling cadence in §9.
12. **Resilience**: a failure fetching/parsing one target, or a failure refreshing either reference catalog (fall back to the last successfully cached version), must not prevent other targets or notifications from proceeding — log/report the failure rather than crashing the whole run.

## 9. Non-functional requirements
- **Notification latency**: target under ~20–30 minutes from a restock going live to the Telegram alert arriving, since restocks can sell out within minutes.
- **Unified polling**: all retailer targets are checked on the same schedule (no per-retailer frequency tiering, unlike v1) — proposed every 20–30 minutes, balanced against not hammering any single site too hard.
- **No duplicate spam**: a still-in-stock, unchanged-price match must not generate more than one individual notification; an unchanged above-MSRP digest must not re-send on every poll (§8.10).
- **Respect target sites**: reasonable request rate per site, appropriate User-Agent; Amazon.sg is explicitly best-effort/lower-priority given its ToS stance on scraping.
- **Config over code changes**: adding a new retailer should not require rewriting core logic — the target list should be simple, edit-friendly configuration.
- **No stored credentials of any kind** — with cart automation out of scope (§4), there are no saved login sessions to protect. Only the Telegram bot token/chat ID are stored, as environment/secret configuration, never in code.

## 10. Technical groundwork already known (from a prior build of this same idea)
This is a ground-up rewrite, not a continuation of any existing scaffold — but research from prior work on this same problem is still valid and worth carrying forward as implementation guidance, not as constraints:
- Toys"R"Us and DXCOLLECTION are both server-rendered; plain HTTP GET + BeautifulSoup-style parsing works, with specific selectors already verified against live HTML (available on request rather than repeated here, since markup can drift and should be re-verified fresh).
- Lazada and Carousell are JS-rendered and were successfully fetched via Playwright with a headless Chromium browser in the prior build — no paid service or unusual tooling needed.
- The Lazada storefront's root URL only shows a small "feed" (1 product) — the full catalog lives under its "All-Products" tab (URL included in §6).
- pokemon-card.com/products/ (§7.2) is confirmed live and working as the Japanese reference source — checked directly during this PRD's research, publishes official JPY suggested-retail-price per release with a filterable product-type view (拡張パック = expansion pack/booster).

## 11. Open decisions / inputs needed
1. **Telegram bot token + chat ID** — the project was fully torn down (repos deleted, local files removed); these need to be provided again before any notification can be sent. Provide via a gitignored local secrets file or env var, never pasted in plain chat.
2. **Reference-catalog refresh cadence** — proposed every 4 hours for both catalogs (§7.1, §7.2); confirm or adjust.
3. **Exchange-rate refresh cadence** — proposed daily (§7.2); confirm or adjust.
4. **Unified poll interval** — proposed every 20–30 minutes (§9); confirm or adjust.
5. **Fuzzy-match similarity threshold** — implementation detail (§7.5), to be tuned against real catalog data once built; not a blocking product decision.
6. **Japanese box-pricing assumption** (pack price × 30 = box price, §7.2) — to be spot-checked against a couple of real box listings once built; may need adjustment for special/deluxe releases with different pack counts.
7. **Japanese set-name mapping list** — needs to be built (katakana ↔ English-romanized set name, per §7.2) and kept current as new sets release; not yet started.
8. **Deployment/scheduling target** — not yet decided for this rebuild. Options already explored previously: GitHub Actions (scheduled workflows, zero new accounts needed, but can't run an always-on process), a persistent VM (Oracle Cloud Free Tier — genuinely free, more manual setup), or Fly.io (simpler PaaS, but needs a card on file). Since this version has no always-on requirement (no Telegram `/admin` bot in scope here), GitHub Actions is likely the simplest fit again — to be proposed and confirmed once the app is built and tested locally.
9. **Additional retailers** — Popular/Challenger, TikTok Shop, and further local/secondary shops beyond DXCOLLECTION are candidates if confirmed listing URLs are supplied.
10. **Digest cap (N)** — proposed top 5 closest-to-MSRP items per retailer digest, plus a total count of the rest (§8.10); confirm or adjust.

## 12. Deferred / future scope
- **Cart reservation / auto-add-to-cart** (Tier 1 retailers: Lazada, Toys"R"Us): explicitly out of scope for this version (§4), but was scoped in detail in v1 of this PRD before being deferred — saved-session-cookie approach, no stored payment/credentials, never auto-completing checkout. Worth revisiting once the core detection/notification system is proven reliable.
- A Telegram-based admin/config interface (e.g. commands to inspect or override the reference catalogs) was explored in a prior build attempt but is not part of this version's scope.

## 13. Success criteria
- Given a retailer lists a sealed product in stock, and it confidently matches a product in the appropriate live reference catalog (§7.1 English or §7.2 Japanese, by language) at or below that catalog's price, an **individual** Telegram message with the correct product name, price, matched reference price, and a working listing link arrives within the target polling interval.
- Given a retailer has one or more in-stock, catalog-matched listings priced above their reference, a single **digest** message for that retailer arrives showing the closest-to-MSRP items (capped, §11.10) with their % above MSRP each, and a total count — not one message per listing.
- Given a listing has no confident catalog match, no notification of any kind is sent and nothing is written to state for it.
- Re-polling the same still-in-stock, same-price individual match does not produce a second individual notification.
- Re-polling a retailer whose above-MSRP digest contents haven't changed does not re-send that digest.
- A parsing, matching, or catalog-refresh failure (English or Japanese catalog, or the exchange-rate fetch) on one retailer does not stop checks or notifications on the others.
- No scenario involves cart automation, stored credentials, or any action beyond sending a Telegram message — this version only ever informs, never acts on the user's behalf on a retailer's site.
