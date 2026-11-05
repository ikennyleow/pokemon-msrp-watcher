# Carousell watcher on your Mac

Carousell blocks cloud servers, so the GitHub Action can't check it. This
runs the Carousell part of the watcher on your Mac every 30 minutes and
sends alerts to the same Telegram bot.

It checks:

- **Sealed product**: newest-first Carousell searches for booster boxes,
  Chinese booster boxes, Elite Trainer Boxes, booster bundles and premium
  collections, priced against resale value the same way as the GitHub run
  (TCGplayer market for English and Japanese, resale list for Chinese).
- **Buyout lots**: newest listings for "pokemon binder", "pokemon
  collection", "old pokemon cards", "pokemon cards lot" and "pokemon cards
  clearance". You get one message per newly posted lot (S$20–400) with a
  link. Empty binders, sealed product, graded cards, wanted ads and dealers'
  keyword-stuffed titles are filtered out.

## Set up (once, about 5 minutes)

In Terminal:

```bash
git clone https://github.com/ikennyleow/pokemon-msrp-watcher.git ~/pokemon-msrp-watcher
cd ~/pokemon-msrp-watcher
./mac/setup_mac.sh
```

It installs what it needs into the folder, asks for the Telegram bot token
and your chat ID (the same ones saved in GitHub), does a first run, and
schedules the rest. The first run only records what's already listed, so you
don't get a flood of old listings.

## Day to day

- Runs every 30 minutes while the Mac is awake. If it was asleep, it runs
  again when it wakes.
- Each run pulls the latest code and `chinese_prices.json` from GitHub first,
  so changes reach the Mac automatically.
- Log: `~/pokemon-msrp-watcher/logs/watcher.log`
- Stop it: `./mac/uninstall_mac.sh`
