# SparkGrid Web Client — Setup Guide

## Overview

SparkGrid is an Instagram account management automation platform. It runs on Windows
and uses Camoufox (anti-detect Firefox) for browser automation, a FastAPI backend
for orchestration, and a Telegram bot for remote control.

## Repository Structure

```
patches/          — all project source code (57 .py + 3 .html + tests)
  app.py          — FastAPI server, all API endpoints
  connection_scheduler.py — multi-account scheduling, proxy lane management
  instagram_web_profile_workflow.py — login, consent, warmup, upload, make_public
  instagram_web_upload.py — reel upload + warmup logic
  telegram_bot.py — Telegram bot client
  story_trigger.py — automatic story posting on view threshold
  ads_power_checker.py — metrics collection via Ads Power browser
  browser_launcher.py — Camoufox/SparkBrowser profile management
  index.html      — main dashboard UI
  ui/index.html   — alternate dashboard UI (with Dashboard button)
  ui/dashboard.html — dashboard-only UI
AGENTS.md         — shared memory between Claude/Qwen/Hermes agents
.gitignore        — excludes third-party packages, DBs, secrets, logs
```

## Prerequisites

### Python
- Python 3.11+ (bundled at `python/python.exe` in production installs)
- Packages: see `patches/` — third-party packages are NOT in git. Install:
  ```
  pip install fastapi uvicorn playwright pydantic pyotp aiohttp requests
  pip install camoufox[geoip] browserforge pillow lxml pyyaml
  pip install python-telegram-bot[job-queue] anthropic
  pip install cryptography numpy imageio-ffmpeg platformdirs PySocks
  ```
  Full list (all 20 packages):
  `fastapi uvicorn playwright pydantic pyotp aiohttp requests camoufox[geoip] browserforge pillow lxml pyyaml python-telegram-bot[job-queue] anthropic cryptography numpy imageio-ffmpeg platformdirs PySocks`
- Playwright browsers: `python -m playwright install chromium`
- Camoufox: `python -m camoufox fetch`

### External Tools
- **Ads Power** browser (for metrics collection): `http://localhost:50325`
- **Camoufox** (anti-detect Firefox): bundled or installed via `camoufox fetch`

## Secrets

Create `secrets.local.ps1` (NOT in git, see `.gitignore`) in the service directories:

```powershell
# C:\Users\<user>\SparkGrid-services\software\secrets.local.ps1
# C:\Users\<user>\SparkGrid-services\bot\secrets.local.ps1
$env:ANTHROPIC_API_KEY = "sk-..."           # for vision fallback
$env:ANTHROPIC_BASE_URL = "https://api.apiyi.com"  # NO /v1 suffix
$env:TELEGRAM_BOT_TOKEN = "123456:ABC-DEF..."
$env:TELEGRAM_CHAT_ID = "123456789"
$env:ADS_POWER_API_URL = "http://localhost:50325"
```

## Data Directory

Set `SPARKGRID_DATA_DIR` environment variable to a writable path:
```
SPARKGRID_DATA_DIR=C:\Users\<user>\AppData\Local\SparkGrid\data
```

Contains:
- `bot.db` — SQLite database (accounts, jobs, proxies, metrics)
- `logs/` — automation, server, bot, story_trigger, metrics logs
- `ai_content_data/debug/` — per-run diagnostics (screenshots, DOM dumps)
- `browser_profiles/` — Camoufox profile directories

## Launch Scripts

PowerShell launchers (not in git, create manually):
```
SparkGrid-services/software/start_software.ps1  — starts app.py with auto-restart
SparkGrid-services/software/stop_software.ps1   — recursive tree kill + port wait
SparkGrid-services/bot/start_bot.ps1           — starts telegram_bot.py with auto-restart
SparkGrid-services/bot/stop_bot.ps1            — recursive tree kill + 5s Telegram settle
```

## Quick Start

### Automated install (recommended for clean Windows servers)

1. Clone the repo
2. Run `install.ps1` as Administrator — installs Python, all dependencies, Camoufox/Playwright, service scripts, disables screen saver
3. Open `http://127.0.0.1:8770/setup` in a browser
4. Enter your API keys (Telegram token, chat ID, Anthropic key, AdsPower key)
5. Click "Проверить" to verify each key
6. Click "Перезапустить бота" to start the bot with the new keys
7. Dashboard: `http://127.0.0.1:8770/dashboard`

### Manual install

1. Clone the repo
2. Install Python 3.11+ and all dependencies (see above)
3. Run `python -m playwright install chromium` and `python -m camoufox fetch`
4. Create `secrets.local.ps1` with your API keys (see below)
5. Set `SPARKGRID_DATA_DIR` 
6. Copy `patches/*.py` to your install `_internal/` directory
7. Run: `python app.py` (starts FastAPI on port 8770)
8. Dashboard: `http://127.0.0.1:8770/dashboard`

### Setup page (key entry without secrets.local.ps1)

If `secrets.local.ps1` is absent, keys can be entered via the web UI at `/setup`.
Keys are stored in the `ads_power_config` table in `bot.db`. `start_bot.ps1` reads
from this table when `secrets.local.ps1` is not found.

## Known Issues

- **RDP disconnect freezes browser**: When RDP session disconnects, the virtual
  GPU (Red Hat VirtIO) stops rendering, and Camoufox hangs indefinitely.
  Workaround: disable screen saver, keep RDP session active, or use headless mode.
- **No process watchdog**: A hung browser process blocks ProcessManager indefinitely.
  Manual `/stop` via Telegram bot is the only recovery.
- **Timezone mismatch**: `started_at` uses SQLite UTC, `finished_at` uses Python local.
