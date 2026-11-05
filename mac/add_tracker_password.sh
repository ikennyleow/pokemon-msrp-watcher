#!/bin/bash
# Saves your tracker password to secrets.json so the daily price check can
# read your holdings (read-only). Run once:  ./mac/add_tracker_password.sh
set -euo pipefail
cd "$(dirname "$0")/.."
read -r -s -p "Tracker password (hidden): " PW; echo
PW="$PW" .venv/bin/python - <<'PY'
import json, os
f = "secrets.json"
d = json.load(open(f)) if os.path.exists(f) else {}
d["tracker_password"] = os.environ["PW"]
old = os.umask(0o077)
json.dump(d, open(f, "w"), indent=2)
os.umask(old)
os.chmod(f, 0o600)
print("Saved. The daily price check runs on the first watcher run after 9am.")
PY
