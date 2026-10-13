#!/bin/zsh
# Remove the Sponsor Skipper LaunchAgent (stops the server; the repo and caches are untouched).
set -uo pipefail
LABEL="com.sponsor-skipper.server"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null && echo "stopped $LABEL" || echo "$LABEL was not loaded"
if [[ -f "$DEST" ]]; then rm "$DEST" && echo "removed $DEST"; fi
echo "log left in place: $HOME/Library/Logs/sponsor-skipper.log"
