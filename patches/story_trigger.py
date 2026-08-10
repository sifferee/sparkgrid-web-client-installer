"""
Story Auto-Trigger.

Monitors account metrics and automatically posts Stories when a Reel
hits 10k-23k views. After the first trigger, posts once per day with
±10-60 min randomization.

Logic:
  1. After each metrics check cycle, scan for accounts with a Reel
     that has views >= threshold (random 10k-23k per account per day).
  2. If triggered and no Story posted today → post Story.
  3. After first trigger: once per 20-26h, check if last Story > 20-26h ago.
  4. Story images come from story_library (already in software).
  5. Story settings (caption, link, sticker) from story_settings.

Architecture:
  - Uses existing POST /api/ig-web-upload/post-story endpoint
  - Uses existing story_library for images
  - Records triggers in story_triggers table
  - Runs after each metrics check cycle
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.request import urlopen, Request
from urllib.error import URLError

# ─── Config ───────────────────────────────────────────────────────────────────

DATA_DIR = Path(os.environ.get("SPARKGRID_DATA_DIR") or Path(__file__).resolve().parent.parent / "data").resolve()
DB_PATH = DATA_DIR / "bot.db"
LOG_DIR = DATA_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

SPARKGRID_API = os.environ.get("SPARKGRID_API_URL", "http://127.0.0.1:8770")

# Threshold range for triggering — first reel must hit 10-23K views
VIEWS_THRESHOLD_MIN = 10000
VIEWS_THRESHOLD_MAX = 23000

# Daily story interval — 24h base + 10-60 min random spread
# Instagram detects exact 24h patterns; the spread prevents that
DAILY_INTERVAL_HOURS = 24
DAILY_SPREAD_MIN_SEC = 600   # 10 min
DAILY_SPREAD_MAX_SEC = 3600  # 60 min

# Delay between story posts across accounts — disabled
# Each account runs through its own browser/proxy, no correlation visible to Instagram
INTER_ACCOUNT_DELAY_MIN = 0
INTER_ACCOUNT_DELAY_MAX = 0


# ─── Logging ──────────────────────────────────────────────────────────────────

def log(msg: str, level: str = "INFO") -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} {level}: [story-trigger] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_DIR / "story_trigger.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ─── DB Schema ─────────────────────────────────────────────────────────────────

def _db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_story_trigger_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS story_triggers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_name TEXT NOT NULL,
            trigger_reel_pk TEXT DEFAULT '',
            trigger_views INTEGER DEFAULT 0,
            story_posted_at TEXT,
            story_job_id INTEGER DEFAULT 0,
            trigger_type TEXT DEFAULT 'views_threshold',
            threshold_used INTEGER DEFAULT 0,
            error TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_story_triggers_account
        ON story_triggers(account_name, story_posted_at)
    """)
    conn.commit()


# ─── Core Logic ───────────────────────────────────────────────────────────────

def get_random_threshold() -> int:
    """Random threshold per check to avoid pattern."""
    return random.randint(VIEWS_THRESHOLD_MIN, VIEWS_THRESHOLD_MAX)


def get_daily_interval_seconds() -> float:
    """24h base + random spread 10min-1h."""
    base = DAILY_INTERVAL_HOURS * 3600
    spread = random.uniform(DAILY_SPREAD_MIN_SEC, DAILY_SPREAD_MAX_SEC)
    return base + spread


def has_story_today(conn: sqlite3.Connection, account_name: str) -> bool:
    """Check if a Story was already posted today for this account."""
    row = conn.execute("""
        SELECT story_posted_at FROM story_triggers
        WHERE account_name = ?
        AND story_posted_at IS NOT NULL
        AND date(story_posted_at) = date('now')
        ORDER BY story_posted_at DESC LIMIT 1
    """, (account_name,)).fetchone()
    return row is not None


def get_last_story_time(conn: sqlite3.Connection, account_name: str) -> str:
    """Get last story_posted_at for account, or empty string."""
    row = conn.execute("""
        SELECT story_posted_at FROM story_triggers
        WHERE account_name = ?
        AND story_posted_at IS NOT NULL
        ORDER BY story_posted_at DESC LIMIT 1
    """, (account_name,)).fetchone()
    return str(row["story_posted_at"]) if row else ""


def should_post_daily_story(conn: sqlite3.Connection, account_name: str) -> bool:
    """Check if enough time passed since last Story (20-26h)."""
    last = get_last_story_time(conn, account_name)
    if not last:
        return False  # No prior trigger — wait for views threshold
    try:
        last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return False
    elapsed = (datetime.now() - last_dt).total_seconds()
    interval = get_daily_interval_seconds()
    return elapsed >= interval


def get_latest_metrics(conn: sqlite3.Connection, account_name: str) -> dict[str, Any]:
    """Get latest metrics snapshot for account."""
    row = conn.execute("""
        SELECT * FROM account_metrics_snapshots
        WHERE account_name = ?
        ORDER BY checked_at DESC LIMIT 1
    """, (account_name,)).fetchone()
    return dict(row) if row else {}


def get_per_post_views(conn: sqlite3.Connection, account_name: str) -> list[dict[str, Any]]:
    """Get per-post views from latest snapshot."""
    row = conn.execute("""
        SELECT per_post_json FROM account_metrics_snapshots
        WHERE account_name = ?
        ORDER BY checked_at DESC LIMIT 1
    """, (account_name,)).fetchone()
    if not row or not row["per_post_json"]:
        return []
    try:
        posts = json.loads(row["per_post_json"])
        if isinstance(posts, list):
            return posts
    except Exception:
        pass
    return []


def find_trigger_reel(posts: list[dict[str, Any]], threshold: int) -> dict[str, Any] | None:
    """Find a reel with views >= threshold. Returns the post with highest views."""
    candidates = [p for p in posts if int(p.get("views", 0)) >= threshold]
    if not candidates:
        return None
    # Return the one with most views
    return max(candidates, key=lambda p: int(p.get("views", 0)))


def record_trigger(
    conn: sqlite3.Connection,
    account_name: str,
    reel_pk: str,
    views: int,
    threshold: int,
    story_job_id: int = 0,
    error: str = "",
) -> None:
    """Record a story trigger."""
    now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    story_posted_at = now_iso if story_job_id else None
    conn.execute("""
        INSERT INTO story_triggers
            (account_name, trigger_reel_pk, trigger_views, story_posted_at,
             story_job_id, threshold_used, error)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (account_name, reel_pk, views, story_posted_at, story_job_id, threshold, error))
    conn.commit()


def record_trigger_pending(
    conn: sqlite3.Connection,
    account_name: str,
    reel_pk: str,
    views: int,
    threshold: int,
) -> None:
    """Record a trigger without posting (Story posting will be attempted)."""
    conn.execute("""
        INSERT INTO story_triggers
            (account_name, trigger_reel_pk, trigger_views, threshold_used, story_posted_at)
        VALUES (?, ?, ?, ?, NULL)
    """, (account_name, reel_pk, views, threshold))
    conn.commit()


def trigger_story_post(account_name: str) -> dict[str, Any]:
    """Call SparkGrid API to post a Story for account.

    The post-story endpoint reads form data (request.form()), not JSON.
    We must send accounts as a form field, not a JSON body.
    """
    url = f"{SPARKGRID_API}/api/ig-web-upload/post-story"
    try:
        from urllib.parse import urlencode
        form_data = urlencode({"accounts": account_name}).encode("utf-8")
        req = Request(url, data=form_data, method="POST", headers={"Content-Type": "application/x-www-form-urlencoded"})
        resp = urlopen(req, timeout=120)
        data = json.loads(resp.read())
        return data
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_story_ready_accounts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Get all logged_in accounts with story library available."""
    rows = conn.execute("""
        SELECT DISTINCT a.name as account_name
        FROM accounts a
        WHERE a.web_upload_login_status = 'logged_in'
        AND a.enabled = 1
        AND a.name IN (
            SELECT DISTINCT account_name FROM api_content_assets
            WHERE status IN ('ready', 'uploaded')
        )
        ORDER BY a.name
    """).fetchall()
    return [dict(r) for r in rows]


def run_trigger_check() -> int:
    """Check all accounts for story triggers. Returns count of stories posted."""
    conn = _db_conn()
    posted = 0
    try:
        ensure_story_trigger_schema(conn)

        accounts = get_story_ready_accounts(conn)
        if not accounts:
            log("No story-ready accounts found.")
            return 0

        # Shuffle account order every cycle — prevents fixed sequential pattern
        import random as _rng
        _rng.shuffle(accounts)

        log(f"Checking {len(accounts)} accounts for story triggers")

        for acc in accounts:
            name = acc["account_name"]

            # Skip if already posted today
            if has_story_today(conn, name):
                continue

            # Get latest metrics
            metrics = get_latest_metrics(conn, name)
            if not metrics:
                continue

            # Get per-post views
            posts = get_per_post_views(conn, name)
            if not posts:
                continue

            # Check if we should post daily story (after first trigger)
            last_story = get_last_story_time(conn, name)
            threshold = get_random_threshold()

            if last_story:
                # Already triggered before — check daily interval
                if not should_post_daily_story(conn, name):
                    continue
                # Time for daily story
                log(f"@{name}: daily story due (last={last_story})")
            else:
                # First trigger — check views threshold
                trigger_reel = find_trigger_reel(posts, threshold)
                if not trigger_reel:
                    continue
                views = int(trigger_reel.get("views", 0))
                reel_pk = str(trigger_reel.get("pk", ""))
                log(f"@{name}: TRIGGER! reel {reel_pk} has {views} views (threshold={threshold})")

            # Post the story
            result = trigger_story_post(name)
            if result.get("ok") and result.get("started", True) is not False:
                job_id = int(result.get("story_job_id", 0) or result.get("run_id", 0) or 0)
                # Record trigger
                if last_story:
                    # Daily trigger
                    record_trigger(conn, name, "", 0, 0, job_id)
                else:
                    # First trigger
                    trigger_reel = find_trigger_reel(posts, threshold)
                    if trigger_reel:
                        record_trigger(
                            conn, name,
                            str(trigger_reel.get("pk", "")),
                            int(trigger_reel.get("views", 0)),
                            threshold,
                            job_id,
                        )
                posted += 1
                log(f"@{name}: ✅ Story posted (job_id={job_id})")

                # No delay between accounts — each has own proxy/browser
                if INTER_ACCOUNT_DELAY_MAX > 0:
                    delay = random.uniform(INTER_ACCOUNT_DELAY_MIN, INTER_ACCOUNT_DELAY_MAX)
                    log(f"Waiting {delay:.0f}s before next account")
                    time.sleep(delay)
            else:
                error = str(result.get("error") or result.get("reason") or result.get("message") or "unknown")
                log(f"@{name}: ❌ Story post failed: {error}", "ERROR")
                record_trigger(conn, name, "", 0, threshold, 0, error)

    except Exception as e:
        log(f"Trigger check error: {e}", "ERROR")
    finally:
        conn.close()

    log(f"Story trigger cycle: {posted} stories posted")
    return posted


def start_trigger_thread() -> "threading.Thread":
    """Start trigger checker in background thread."""
    import threading
    def _loop():
        log("Story trigger checker started")
        while True:
            try:
                run_trigger_check()
            except Exception as e:
                log(f"Loop error: {e}", "ERROR")
            # Random interval 5-43 min — prevents fixed check pattern
            time.sleep(random.randint(300, 2580))

    t = threading.Thread(target=_loop, daemon=True, name="story-trigger")
    t.start()
    return t


if __name__ == "__main__":
    posted = run_trigger_check()
    print(f"Posted {posted} stories")
