#!/bin/bash
# Stops the scheduled Carousell watcher on this Mac. Leaves the code,
# secrets.json and logs in place.
LABEL="sg.kayley.carousell-watcher"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null && echo "Stopped." || echo "It wasn't running."
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
