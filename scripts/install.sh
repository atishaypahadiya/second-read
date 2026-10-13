#!/bin/zsh
# Install the Second Read server as a macOS LaunchAgent: starts at login, restarts on crash.
#
#   scripts/install.sh              install (asks for the extension ID once if it is not in .env)
#   scripts/install.sh --dry-run    print the plist it would install; changes nothing
#
# The extension ID locks the local API to your extension: put  SR_EXTENSION_ID=<id>  in .env at the repo root
# (gitignored), or pass it in the environment, or type it when asked. Find it on chrome://extensions with
# Developer mode on: the "ID" line under Second Read.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.second-read.server"
OLD_LABEL="com.sponsor-skipper.server"          # the name before the rename
AGENTS="$HOME/Library/LaunchAgents"
DEST="$AGENTS/$LABEL.plist"
PYTHON="${PYTHON:-$(command -v python3)}"
LOG="$HOME/Library/Logs/second-read.log"
OLD_LOG="$HOME/Library/Logs/sponsor-skipper.log"
ENV_FILE="${SR_ENV_FILE:-$REPO/.env}"
DRY_RUN=0; [[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

if [[ -z "$PYTHON" ]]; then echo "python3 not found; set PYTHON=/path/to/python3" >&2; exit 1; fi
"$PYTHON" -c 'import sys; assert sys.version_info >= (3, 9), sys.version' || { echo "need python3 >= 3.9" >&2; exit 1; }

# ---- extension ID: environment, then .env, then ask (only on a terminal) --------------------------------------
valid_id() { [[ "$1" =~ ^[a-p]{32}$ ]]; }
EXT_ID="${SR_EXTENSION_ID:-}"; EXT_SRC="the environment"
if [[ -z "$EXT_ID" && -f "$ENV_FILE" ]]; then
  # read the value without executing the file
  EXT_ID="$(sed -n 's/^[[:space:]]*\(export[[:space:]]\{1,\}\)\{0,1\}SR_EXTENSION_ID[[:space:]]*=[[:space:]]*//p' "$ENV_FILE" | tail -1 | tr -d "\"' \r")"
  EXT_SRC="$ENV_FILE"
fi
if [[ -z "$EXT_ID" && -t 0 ]]; then
  echo "Extension ID: open chrome://extensions, turn on Developer mode, copy the ID shown under Second Read."
  echo "It locks the local API to your extension. Press Enter to skip (any extension can then call the API)."
  read -r "EXT_ID?Extension ID: " || EXT_ID=""
  EXT_ID="${EXT_ID//[[:space:]]/}"; EXT_SRC="your answer"
  if [[ -n "$EXT_ID" ]] && valid_id "$EXT_ID" && (( ! DRY_RUN )); then
    { [[ -f "$ENV_FILE" ]] && grep -v '^[[:space:]]*\(export[[:space:]]\{1,\}\)\{0,1\}SR_EXTENSION_ID[[:space:]]*=' "$ENV_FILE" || true; echo "SR_EXTENSION_ID=$EXT_ID"; } > "$ENV_FILE.tmp"
    mv "$ENV_FILE.tmp" "$ENV_FILE"; chmod 600 "$ENV_FILE"
    echo "saved to $ENV_FILE (gitignored), so the next install does not ask"
  fi
fi
if [[ -n "$EXT_ID" ]] && ! valid_id "$EXT_ID"; then
  echo "SR_EXTENSION_ID from $EXT_SRC is not an extension ID (expected 32 letters a-p): '$EXT_ID'" >&2; exit 1
fi

# ---- render the plist ------------------------------------------------------------------------------------------
render() {
  if [[ -n "$EXT_ID" ]]; then
    sed -e "s|__PYTHON__|$PYTHON|g" -e "s|__REPO__|$REPO|g" -e "s|__HOME__|$HOME|g" \
        -e "s|^\([[:space:]]*\)<!-- __EXTENSION_ID_ENV__.*-->|\1<key>SR_EXTENSION_ID</key>\\
\1<string>$EXT_ID</string>|" "$REPO/scripts/$LABEL.plist"
  else
    sed -e "s|__PYTHON__|$PYTHON|g" -e "s|__REPO__|$REPO|g" -e "s|__HOME__|$HOME|g" -e "/__EXTENSION_ID_ENV__/d" "$REPO/scripts/$LABEL.plist"
  fi
}
if (( DRY_RUN )); then
  TMP="$(mktemp -t second-read-plist)"; render > "$TMP"; plutil -lint "$TMP" >/dev/null; cat "$TMP"; rm -f "$TMP"
  echo "--- dry run: nothing installed. extension ID: ${EXT_ID:-<none: API open to any extension>}" >&2
  exit 0
fi

mkdir -p "$AGENTS" "$(dirname "$LOG")"

# Migrate from the old name: stop and remove the old agent (it would fight for port 8765), keep its log.
if launchctl bootout "gui/$(id -u)/$OLD_LABEL" 2>/dev/null; then echo "stopped old agent $OLD_LABEL"; fi
if [[ -f "$AGENTS/$OLD_LABEL.plist" ]]; then rm "$AGENTS/$OLD_LABEL.plist" && echo "removed $AGENTS/$OLD_LABEL.plist"; fi
if [[ -f "$OLD_LOG" && ! -f "$LOG" ]]; then mv "$OLD_LOG" "$LOG" && echo "moved $OLD_LOG -> $LOG"; fi

render > "$DEST"
chmod 600 "$DEST"
plutil -lint "$DEST" >/dev/null

# Replace a previous installation quietly, then load.
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$DEST"
launchctl kickstart -k "gui/$(id -u)/$LABEL"

echo "installed $DEST"
echo "python:  $PYTHON"
echo "repo:    $REPO   (the agent runs from this path: run install.sh again after moving or renaming the folder)"
echo "log:     $LOG"
if [[ -n "$EXT_ID" ]]; then
  echo "api:     locked to extension $EXT_ID (from $EXT_SRC). The ID changes if the extension folder is moved:"
  echo "         update SR_EXTENSION_ID in $ENV_FILE and run install.sh again."
else
  echo "api:     OPEN to any browser extension. Add SR_EXTENSION_ID=<id> to $ENV_FILE and run install.sh again to lock it."
fi
sleep 2
if curl -sf http://127.0.0.1:8765/health >/dev/null; then
  echo "server:  up — $(curl -s http://127.0.0.1:8765/health)"
else
  echo "server:  not responding yet; check: tail -f $LOG"
fi
