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
  ```
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

1. Clone the repo
2. Install Python dependencies (see above)
3. Create `secrets.local.ps1` with your API keys
4. Set `SPARKGRID_DATA_DIR` 
5. Copy `patches/*.py` to your install `_internal/` directory
6. Run: `python app.py` (starts FastAPI on port 8770)
7. Dashboard: `http://127.0.0.1:8770/dashboard`

## Known Issues

- **RDP disconnect freezes browser**: When RDP session disconnects, the virtual
  GPU (Red Hat VirtIO) stops rendering, and Camoufox hangs indefinitely.
  Workaround: disable screen saver, keep RDP session active, or use headless mode.
- **No process watchdog**: A hung browser process blocks ProcessManager indefinitely.
  Manual `/stop` via Telegram bot is the only recovery.
- **Timezone mismatch**: `started_at` uses SQLite UTC, `finished_at` uses Python local.
