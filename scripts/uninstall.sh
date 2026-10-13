#!/bin/zsh
# Remove the Second Read LaunchAgent (stops the server; the repo and caches are untouched).
# Boots out BOTH the current label and the pre-rename one, so nothing from either name is left running.
set -uo pipefail
AGENTS="$HOME/Library/LaunchAgents"
for LABEL in com.second-read.server com.sponsor-skipper.server; do
  if launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null; then echo "stopped $LABEL"; else echo "$LABEL was not loaded"; fi
  if [[ -f "$AGENTS/$LABEL.plist" ]]; then rm "$AGENTS/$LABEL.plist" && echo "removed $AGENTS/$LABEL.plist"; fi
done
for LOG in "$HOME/Library/Logs/second-read.log" "$HOME/Library/Logs/sponsor-skipper.log"; do
  [[ -f "$LOG" ]] && echo "log left in place: $LOG"
done
exit 0
