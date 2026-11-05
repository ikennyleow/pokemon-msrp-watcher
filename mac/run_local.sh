#!/bin/bash
# One local (Mac) run: pull the latest code and price lists, then check
# Carousell. Scheduled every 30 minutes by launchd (see setup_mac.sh).
set -u
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs

# Keep the log small: drop it once it passes ~1 MB.
if [ -f logs/watcher.log ] && [ "$(wc -c < logs/watcher.log)" -gt 1000000 ]; then
  mv logs/watcher.log logs/watcher.old.log
fi

{
  echo "=== $(date '+%Y-%m-%d %H:%M:%S')"
  # Fresh catalogs and chinese_prices.json come from GitHub. A failed pull
  # (offline, just woke up) is fine: run with what's here.
  git pull --ff-only -q 2>&1 || echo "[warn] git pull failed, using local copy"
  WATCHER_MODE=local .venv/bin/python pokemon_msrp_watcher.py 2>&1
} >> logs/watcher.log
