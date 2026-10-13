#!/bin/zsh
# Install the Sponsor Skipper server as a macOS LaunchAgent: starts at login, restarts on crash.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.sponsor-skipper.server"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
PYTHON="${PYTHON:-$(command -v python3)}"
LOG="$HOME/Library/Logs/sponsor-skipper.log"

if [[ -z "$PYTHON" ]]; then echo "python3 not found; set PYTHON=/path/to/python3" >&2; exit 1; fi
"$PYTHON" -c 'import sys; assert sys.version_info >= (3, 9), sys.version' || { echo "need python3 >= 3.9" >&2; exit 1; }

mkdir -p "$HOME/Library/LaunchAgents" "$(dirname "$LOG")"
sed -e "s|__PYTHON__|$PYTHON|g" -e "s|__REPO__|$REPO|g" -e "s|__HOME__|$HOME|g" \
    "$REPO/scripts/$LABEL.plist" > "$DEST"
plutil -lint "$DEST" >/dev/null

# Replace a previous installation quietly, then load.
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$DEST"
launchctl kickstart -k "gui/$(id -u)/$LABEL"

echo "installed $DEST"
echo "python:  $PYTHON"
echo "repo:    $REPO"
echo "log:     $LOG"
sleep 2
if curl -sf http://127.0.0.1:8765/health >/dev/null; then
  echo "server:  up — $(curl -s http://127.0.0.1:8765/health)"
else
  echo "server:  not responding yet; check: tail -f $LOG"
fi
