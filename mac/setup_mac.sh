#!/bin/bash
# One-time setup of the Carousell watcher on this Mac.
# Run from the repo folder:  ./mac/setup_mac.sh
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$(pwd)"
LABEL="sg.kayley.carousell-watcher"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

echo "== 1/5 Checking Python"
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found. Run: xcode-select --install   then run this script again."
  exit 1
fi
python3 -c 'import sys; assert sys.version_info >= (3, 9), sys.version' \
  || { echo "Python 3.9 or newer needed."; exit 1; }
python3 --version

echo "== 2/5 Installing packages (a minute or two)"
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
.venv/bin/python -m playwright install chromium

echo "== 3/5 Telegram bot"
if [ -f secrets.json ]; then
  echo "secrets.json already exists, keeping it."
else
  echo "Use the same bot as the GitHub watcher. Token: Telegram > @BotFather > /mybots > your bot > API Token."
  read -r -s -p "Bot token (hidden): " TOKEN; echo
  read -r -p "Chat ID (your user id, a number): " CHAT
  umask 077
  printf '{\n  "telegram_bot_token": "%s",\n  "telegram_chat_id": "%s"\n}\n' "$TOKEN" "$CHAT" > secrets.json
  echo "Saved to secrets.json (only readable by you, never committed)."
fi

echo "== 4/5 First run (records what's already listed, so you aren't flooded)"
chmod +x mac/run_local.sh
mac/run_local.sh
tail -n 15 logs/watcher.log

echo "== 5/5 Scheduling every 30 minutes"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$REPO/mac/run_local.sh</string></array>
  <key>StartInterval</key><integer>1800</integer>
  <key>RunAtLoad</key><false/>
  <key>StandardOutPath</key><string>$REPO/logs/launchd.log</string>
  <key>StandardErrorPath</key><string>$REPO/logs/launchd.log</string>
</dict>
</plist>
PL
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo
echo "Done. It runs every 30 minutes while the Mac is awake."
echo "Log:     $REPO/logs/watcher.log"
echo "Stop it: ./mac/uninstall_mac.sh"
