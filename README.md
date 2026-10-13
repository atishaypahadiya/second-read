# Second Read

_Formerly Sponsor Skipper._

A Chrome extension that skips **scripted sponsor reads** on YouTube and lets **integrated sponsors** (the creator
actually using the product) play. Everything runs locally: the transcript comes from YouTube's own caption track,
a local Qwen model (via Ollama) finds the read, and the extension jumps over it with an Undo toast.

No audio download, no third-party APIs, no SponsorBlock data. Bias toward playing: when unsure, nothing is skipped.

## Setup

1. **Ollama + model**
   ```bash
   brew install ollama          # or the Ollama app
   ollama pull qwen3:14b
   ```
2. **Server** (stdlib Python 3.9+, listens on `127.0.0.1:8765`)

   Run it once by hand to check:
   ```bash
   python3 server/server.py
   curl localhost:8765/health
   ```
   Or install it as a LaunchAgent so it starts at login and restarts if it crashes:
   ```bash
   scripts/install.sh           # writes ~/Library/LaunchAgents/com.second-read.server.plist and loads it
   scripts/install.sh --dry-run # prints the plist it would install; changes nothing
   tail -f ~/Library/Logs/second-read.log
   scripts/uninstall.sh         # stops and removes it (and the old com.sponsor-skipper.server agent, if present)
   ```
   `PYTHON=/path/to/python3 scripts/install.sh` picks a specific interpreter. The agent runs from the repo
   directory, so `git pull` + `launchctl kickstart -k gui/$(id -u)/com.second-read.server` picks up changes.
   Run `install.sh` again after moving or renaming the folder (the plist holds the absolute path).

   **Lock the API to your extension.** `install.sh` puts `SR_EXTENSION_ID` into the agent's environment, and the
   server then refuses any request that comes from another extension or from a web page. It takes the ID from,
   in order: the `SR_EXTENSION_ID` environment variable, a `.env` file at the repo root (gitignored; see
   `.env.example`), or a prompt if you run it in a terminal (the answer is saved to `.env`).

   To find the ID: open `chrome://extensions`, turn on **Developer mode** (top right), and copy the **ID** line
   shown under Second Read (32 letters, a to p). Load the extension first (step 3) so the ID exists.
   ```bash
   echo "SR_EXTENSION_ID=<paste the id>" > .env
   scripts/install.sh
   ```
   Running the server by hand, pass it yourself: `SR_EXTENSION_ID=<id> python3 server/server.py`.

   What the lock does and does not do: a request carrying another extension's `Origin`, or a web page's, gets
   a 403. A request with no `Origin` header at all is still answered; that is how `curl` and the dashboard page
   reach the server. An unpacked extension's ID changes when its folder is moved or renamed: update `.env` and
   run `install.sh` again, or the server will refuse your own extension (the popup shows "server offline" or
   an error, and the log says `forbidden extension`).
3. **Extension**: Chrome → `chrome://extensions` → Developer mode → Load unpacked → the `extension/` folder.
   After editing extension files: Reload on the extension card, then refresh the YouTube tab.

## How it works

See `CLAUDE.md` for the full pipeline and the build log, `server/testset.md` for the test videos and results.
Short version: caption lines → prefilter for sponsor cues → Qwen labels candidate line ranges → a second pass
pins the exact first/last word → the extension draws markers on the progress bar (red skip, green keep, yellow
show) and seeks past red ones with a 6s Undo toast.

## Debugging

- Extension: DevTools console on a YouTube tab, filter `SecondRead`.
- Server: its terminal, or `~/Library/Logs/second-read.log` when installed as a LaunchAgent.
- Results per analyzed video: `server/cache/<videoId>.json`; per-run log: `server/cache/results.local.md`.
