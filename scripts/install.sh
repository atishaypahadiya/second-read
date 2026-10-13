#!/bin/zsh
# Install the Second Read server as a macOS LaunchAgent: starts at login, restarts on crash.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.second-read.server"
OLD_LABEL="com.sponsor-skipper.server"          # the name before the rename
AGENTS="$HOME/Library/LaunchAgents"
DEST="$AGENTS/$LABEL.plist"
PYTHON="${PYTHON:-$(command -v python3)}"
LOG="$HOME/Library/Logs/second-read.log"
OLD_LOG="$HOME/Library/Logs/sponsor-skipper.log"

if [[ -z "$PYTHON" ]]; then echo "python3 not found; set PYTHON=/path/to/python3" >&2; exit 1; fi
"$PYTHON" -c 'import sys; assert sys.version_info >= (3, 9), sys.version' || { echo "need python3 >= 3.9" >&2; exit 1; }

mkdir -p "$AGENTS" "$(dirname "$LOG")"

# Migrate from the old name: stop and remove the old agent (it would fight for port 8765), keep its log.
if launchctl bootout "gui/$(id -u)/$OLD_LABEL" 2>/dev/null; then echo "stopped old agent $OLD_LABEL"; fi
if [[ -f "$AGENTS/$OLD_LABEL.plist" ]]; then rm "$AGENTS/$OLD_LABEL.plist" && echo "removed $AGENTS/$OLD_LABEL.plist"; fi
if [[ -f "$OLD_LOG" && ! -f "$LOG" ]]; then mv "$OLD_LOG" "$LOG" && echo "moved $OLD_LOG -> $LOG"; fi

sed -e "s|__PYTHON__|$PYTHON|g" -e "s|__REPO__|$REPO|g" -e "s|__HOME__|$HOME|g" \
    "$REPO/scripts/$LABEL.plist" > "$DEST"
plutil -lint "$DEST" >/dev/null

# Replace a previous installation quietly, then load.
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$DEST"
launchctl kickstart -k "gui/$(id -u)/$LABEL"

echo "installed $DEST"
echo "python:  $PYTHON"
echo "repo:    $REPO   (the agent runs from this path: run install.sh again after moving or renaming the folder)"
echo "log:     $LOG"
sleep 2
if curl -sf http://127.0.0.1:8765/health >/dev/null; then
  echo "server:  up — $(curl -s http://127.0.0.1:8765/health)"
else
  echo "server:  not responding yet; check: tail -f $LOG"
fi
