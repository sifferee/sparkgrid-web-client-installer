"""
Ads Power Account Metrics Checker.

Uses Ads Power browser profiles to fetch Instagram metrics via private API.
One parser profile checks all target accounts per cycle.

Architecture:
  1. Start Ads Power browser (puppeteer/connect CDP)
  2. For each target account: 2 API calls (profile_info + feed)
  3. Save snapshot to DB
  4. Close browser
  5. Sleep with randomization (55-75 min)
  6. Repeat

Traffic per cycle (10 accounts): ~200 KB API + ~5 MB browser open = ~5.2 MB
"""

from __future__ import annotations

import json
import random
import sqlite3
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import urlopen, Request
from urllib.error import URLError

# ─── Config ───────────────────────────────────────────────────────────────────

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" if Path(__file__).resolve().parent.parent.parent.parent.name == "SparkGrid Web Client" else Path(__file__).resolve().parent.parent / "data"

# Try to find DATA_DIR from environment
import os
DATA_DIR = Path(os.environ.get("SPARKGRID_DATA_DIR") or DATA_DIR).resolve()
DB_PATH = DATA_DIR / "bot.db"
LOG_DIR = DATA_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

ADS_POWER_API_URL = os.environ.get("ADS_POWER_API_URL", "http://localhost:50325")
ADS_POWER_PARSER_PROFILES = [p.strip() for p in os.environ.get("ADS_POWER_PARSER_PROFILES", "").split(",") if p.strip()]

CHECKER_ENABLED = os.environ.get("METRICS_CHECKER_ENABLED", "1") == "1"
BASE_INTERVAL_MIN = 55  # minutes
BASE_INTERVAL_MAX = 75  # minutes
# 0 = no limit: check every eligible account in a single cycle (Александр's
# requirement 2026-08-13 — a parser that can't reach every account isn't
# doing its job). Kept as a configurable env override rather than deleted
# outright, so a limit can be reimposed without a code change if a very
# large roster ever makes full cycles impractical.
MAX_TARGETS_PER_CYCLE = int(os.environ.get("METRICS_MAX_TARGETS_PER_CYCLE", "0") or 0)

# ─── Logging ──────────────────────────────────────────────────────────────────

def log(msg: str, level: str = "INFO") -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} {level}: [metrics] {msg}"
    print(line, flush=True)
    try:
        log_file = LOG_DIR / "metrics_checker.log"
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ─── DB Schema ─────────────────────────────────────────────────────────────────

def _db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_metrics_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS account_metrics_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_name TEXT NOT NULL,
            checked_at TEXT NOT NULL,
            followers INTEGER DEFAULT 0,
            following INTEGER DEFAULT 0,
            posts_count INTEGER DEFAULT 0,
            total_likes INTEGER DEFAULT 0,
            total_comments INTEGER DEFAULT 0,
            total_views INTEGER DEFAULT 0,
            active_stories_count INTEGER DEFAULT 0,
            per_post_json TEXT DEFAULT '',
            parser_profile TEXT DEFAULT '',
            error TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ads_power_config (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_metrics_account_checked
        ON account_metrics_snapshots(account_name, checked_at)
    """)
    conn.commit()


def get_config(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM ads_power_config WHERE key=?", (key,)).fetchone()
    return str(row["value"]) if row else default


def set_config(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("""
        INSERT INTO ads_power_config (key, value, updated_at)
        VALUES (?, ?, datetime('now'))
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')
    """, (key, value))
    conn.commit()


# ─── Ads Power Browser Control ────────────────────────────────────────────────

def start_ads_browser(profile_id: str, timeout: int = 30) -> dict[str, Any]:
    """Start Ads Power browser and return CDP endpoint."""
    # Get config from DB
    api_key = ""
    api_url = ADS_POWER_API_URL
    try:
        conn = _db_conn()
        api_key = get_config(conn, "ads_power_api_key", "")
        db_url = get_config(conn, "ads_power_api_url", "")
        if db_url:
            api_url = db_url
        conn.close()
    except Exception:
        pass

    # headless=1: this checker never needs a visible window — every metric
    # comes from API fetches issued inside the page, not from anything
    # rendered on screen. A headless browser uses noticeably less RAM,
    # which matters because AdsPower competes for memory with the upload
    # browsers (16GB VPS, MemoryError observed at 4 parallel Camoufox).
    url = f"{api_url}/api/v1/browser/start?user_id={profile_id}&headless=1"
    log(f"Starting Ads Power browser for profile {profile_id} (url={api_url}, key={'yes' if api_key else 'no'})")
    try:
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        req = Request(url, headers=headers)
        resp = urlopen(req, timeout=timeout)
        data = json.loads(resp.read())
        if data.get("code") != 0:
            return {"ok": False, "error": f"Ads Power error: {data.get('msg', 'unknown')}"}
        ws_url = data.get("data", {}).get("ws", {}).get("selenium", "") or data.get("data", {}).get("ws", {}).get("puppeteer", "")
        if not ws_url:
            # Try direct CDP endpoint
            debug_port = data.get("data", {}).get("debug_port", 0)
            if debug_port:
                ws_url = f"http://127.0.0.1:{debug_port}"
        if not ws_url:
            return {"ok": False, "error": "No CDP endpoint from Ads Power"}
        log(f"Ads Power browser started: {ws_url}")
        return {"ok": True, "ws_url": ws_url, "debug_port": debug_port if 'debug_port' in dir() else 0}
    except URLError as e:
        return {"ok": False, "error": f"Cannot reach Ads Power API: {e}"}
    except Exception as e:
        return {"ok": False, "error": f"Start error: {e}"}


def stop_ads_browser(profile_id: str, timeout: int = 15) -> None:
    """Stop Ads Power browser."""
    api_key = ""
    api_url = ADS_POWER_API_URL
    try:
        conn = _db_conn()
        api_key = get_config(conn, "ads_power_api_key", "")
        db_url = get_config(conn, "ads_power_api_url", "")
        if db_url:
            api_url = db_url
        conn.close()
    except Exception:
        pass

    url = f"{api_url}/api/v1/browser/stop?user_id={profile_id}"
    try:
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        urlopen(Request(url, headers=headers), timeout=timeout)
        log(f"Ads Power browser stopped for profile {profile_id}")
    except Exception as e:
        log(f"Failed to stop Ads Power browser: {e}", "WARNING")


# ─── Playwright Connection ────────────────────────────────────────────────────

def connect_browser(ws_url: str):
    """Connect to running browser via CDP. Returns (browser, context, page).
    
    Uses sync_playwright in a separate thread to avoid asyncio conflicts with FastAPI.
    """
    # Normalize ws_url
    if ws_url and not ws_url.startswith("ws://") and not ws_url.startswith("http"):
        ws_url = f"http://{ws_url}"
    
    import threading
    
    result = {"page": None, "browser": None, "p": None, "error": None}
    
    def _connect():
        try:
            from playwright.sync_api import sync_playwright
            p = sync_playwright().start()
            browser = p.chromium.connect_over_cdp(ws_url)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()
            result["p"] = p
            result["browser"] = browser
            result["page"] = page
            result["_thread_id"] = threading.get_ident()
        except Exception as e:
            result["error"] = str(e)
    
    t = threading.Thread(target=_connect, daemon=True)
    t.start()
    t.join(timeout=15)
    
    if result["error"]:
        raise RuntimeError(f"Cannot connect to browser: {result['error']}")
    if not result["page"]:
        raise RuntimeError("Cannot connect to browser: timeout")
    
    # Return thread info so caller can use same thread for evaluate calls
    return result["p"], result["browser"], None, result["page"], result.get("_thread_id")


def disconnect_browser(p, browser) -> None:
    """Disconnect from browser (does not close Ads Power browser)."""
    try:
        browser.close()
    except Exception:
        pass
    try:
        p.stop()
    except Exception:
        pass


# ─── Instagram Metrics Fetching ──────────────────────────────────────────────

def fetch_profile_metrics(page, username: str) -> dict[str, Any]:
    """Fetch account metrics via Instagram private API.
    
    Returns: {followers, following, posts_count, user_id, is_private, full_name}
    """
    try:
        result = page.evaluate(
            """
            async (username) => {
                const headers = {
                    'X-IG-App-ID': '936619743392459',
                    'X-Requested-With': 'XMLHttpRequest',
                    'Accept': 'application/json',
                };
                const resp = await fetch(
                    '/api/v1/users/web_profile_info/?username=' + encodeURIComponent(username),
                    { credentials: 'include', headers }
                );
                const data = await resp.json();
                const user = (data && data.data && data.data.user) || {};
                return {
                    ok: resp.ok,
                    status: resp.status,
                    user_id: user.id || '',
                    followers: user.edge_followed_by ? user.edge_followed_by.count : 0,
                    following: user.edge_follow ? user.edge_follow.count : 0,
                    posts_count: user.edge_owner_to_timeline_media ? user.edge_owner_to_timeline_media.count : 0,
                    is_private: user.is_private || false,
                    full_name: user.full_name || '',
                    profile_pic: user.profile_pic_url || '',
                };
            }
            """,
            username,
        )
        return result or {"ok": False, "error": "empty response"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def fetch_post_metrics(page, user_id: str, count: int = 12) -> dict[str, Any]:
    """Fetch latest posts with views, likes, comments.
    
    Returns: {total_likes, total_comments, total_views, posts: [...]}
    """
    if not user_id:
        return {"ok": False, "error": "no user_id"}
    try:
        result = page.evaluate(
            """
            async (params) => {
                const headers = {
                    'X-IG-App-ID': '936619743392459',
                    'X-Requested-With': 'XMLHttpRequest',
                    'Accept': 'application/json',
                };
                const resp = await fetch(
                    '/api/v1/feed/user/' + params.user_id + '/?count=' + String(params.count),
                    { credentials: 'include', headers }
                );
                const data = await resp.json();
                const items = (data && data.items) || [];
                const posts = items.map(item => {
                    const caption = item.caption || {};
                    const likes = item.like_count || 0;
                    const comments = item.comment_count || 0;
                    const views = (item.play_count || (item.video_view_count || 0));
                    const pk = item.pk || item.id || '';
                    const taken_at = item.taken_at || 0;
                    const media_type = item.media_type || 0;
                    return { pk, likes, comments, views, taken_at, media_type };
                });
                return {
                    ok: resp.ok,
                    status: resp.status,
                    posts: posts,
                    total_likes: posts.reduce((s, p) => s + p.likes, 0),
                    total_comments: posts.reduce((s, p) => s + p.comments, 0),
                    total_views: posts.reduce((s, p) => s + p.views, 0),
                };
            }
            """,
            {"user_id": str(user_id), "count": int(count)},
        )
        return result or {"ok": False, "error": "empty response"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def fetch_reels_metrics(page, user_id: str) -> dict[str, Any]:
    """Fetch active stories/reels count for account."""
    if not user_id:
        return {"ok": False, "error": "no user_id", "active_stories_count": 0}
    try:
        result = page.evaluate(
            """
            async (userId) => {
                const headers = {
                    'X-IG-App-ID': '936619743392459',
                    'X-Requested-With': 'XMLHttpRequest',
                };
                try {
                    const resp = await fetch(
                        '/api/v1/feed/user/' + userId + '/?count=50&max_id=null&exclude_comment=true&only_feed_first=true',
                        { credentials: 'include', headers }
                    );
                    const data = await resp.json();
                    const items = (data && data.items) || [];
                    const now = Math.floor(Date.now() / 1000);
                    const recent = items.filter(i => (now - (i.taken_at || 0)) < 86400 * 7);
                    const reels = recent.filter(i => i.media_type === 2);
                    return {
                        ok: true,
                        active_stories_count: reels.length,
                        recent_posts: recent.length,
                    };
                } catch(e) {
                    return { ok: false, error: String(e), active_stories_count: 0 };
                }
            }
            """,
            str(user_id),
        )
        return result or {"ok": False, "error": "empty", "active_stories_count": 0}
    except Exception as e:
        return {"ok": False, "error": str(e), "active_stories_count": 0}


# ─── Snapshot Storage ─────────────────────────────────────────────────────────

def save_snapshot(
    account_name: str,
    metrics: dict[str, Any],
    parser_profile: str = "",
    error: str = "",
) -> None:
    # If all key metrics are zero, this is likely a collection failure
    # (Ads Power session lost, Instagram blocked, account restricted) — not a
    # real data point.  Skip saving so the previous valid snapshot remains and
    # delta calculations don't produce false massive drops.
    fol = int(metrics.get("followers", 0))
    views = int(metrics.get("total_views", 0))
    likes = int(metrics.get("total_likes", 0))
    if fol == 0 and views == 0 and likes == 0 and not error:
        log(f"  @{account_name}: all metrics zero — skipping snapshot (likely collection failure)", "WARNING")
        return

    conn = _db_conn()
    try:
        ensure_metrics_schema(conn)
        now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            """
            INSERT INTO account_metrics_snapshots
                (account_name, checked_at, followers, following, posts_count,
                 total_likes, total_comments, total_views, active_stories_count,
                 per_post_json, parser_profile, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account_name, now_iso,
                fol,
                int(metrics.get("following", 0)),
                int(metrics.get("posts_count", 0)),
                likes,
                int(metrics.get("total_comments", 0)),
                views,
                int(metrics.get("active_stories_count", 0)),
                json.dumps(metrics.get("posts", []), ensure_ascii=False)[:5000],
                parser_profile,
                error,
            ),
        )
        conn.commit()
    finally:
        conn.close()


# ─── Main Checker Loop ───────────────────────────────────────────────────────

def get_target_accounts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Get the accounts most overdue for a metrics check.

    Diagnosed 2026-08-13: this used to be `ORDER BY name LIMIT 10`, which
    meant the same first 10 accounts alphabetically were re-checked every
    cycle, forever — every account from "j" onward was NEVER checked at
    all. With 26 accounts that left 16 permanently without metrics; the
    "N accounts without metrics" warning in the bot never went down no
    matter how many times a check was run, because those accounts could
    not physically enter the queue.

    Now ordered by "least recently checked first" (never-checked accounts
    sort first, since NULL is treated as the oldest possible time). By
    default there is no per-cycle limit at all — every eligible account
    is checked each cycle — with the ordering still mattering: it decides
    who gets checked FIRST within the cycle, so the most-overdue accounts
    are covered earliest even if a cycle is interrupted partway through.
    """
    query = """
        SELECT a.name, a.web_upload_login_status, a.web_privacy_status,
               a.web_upload_scale_niche
        FROM accounts a
        LEFT JOIN (
            SELECT account_name, MAX(checked_at) AS last_checked
            FROM account_metrics_snapshots
            GROUP BY account_name
        ) s ON s.account_name = a.name
        WHERE a.web_upload_login_status = 'logged_in'
        AND a.enabled = 1
        ORDER BY (s.last_checked IS NULL) DESC, s.last_checked ASC, a.name ASC
    """
    if MAX_TARGETS_PER_CYCLE > 0:
        rows = conn.execute(query + " LIMIT ?", (MAX_TARGETS_PER_CYCLE,)).fetchall()
    else:
        rows = conn.execute(query).fetchall()
    return [dict(r) for r in rows]


def _run_browser_session(ws_url: str, targets: list[dict[str, Any]], profile_id: str) -> tuple[int, list[str]]:
    """Run all browser operations in a single thread to avoid greenlet conflicts.
    
    Returns (checked_count, errors).
    """
    import threading
    
    # Normalize ws_url
    if ws_url and not ws_url.startswith("ws://") and not ws_url.startswith("http"):
        ws_url = f"http://{ws_url}"
    
    result = {"checked": 0, "errors": [], "done": False}
    
    def _worker():
        try:
            from playwright.sync_api import sync_playwright
            p = sync_playwright().start()
            try:
                browser = p.chromium.connect_over_cdp(ws_url)
                context = browser.contexts[0] if browser.contexts else browser.new_context()
                page = context.pages[0] if context.pages else context.new_page()

                # Close every other tab before starting. AdsPower restores
                # the profile's previous session on launch, so tabs pile up
                # across runs and every one of them reloads Instagram on
                # startup — pure wasted bandwidth, since this checker only
                # ever needs a single tab (all metrics come from API fetches
                # issued through `page`, not from page navigation).
                closed_tabs = 0
                for stale in list(context.pages):
                    if stale is page:
                        continue
                    try:
                        stale.close()
                        closed_tabs += 1
                    except Exception:
                        pass
                if closed_tabs:
                    log(f"Closed {closed_tabs} leftover tab(s) from previous runs")
                
                # Navigate to Instagram first
                try:
                    page.goto("https://www.instagram.com/", wait_until="domcontentloaded", timeout=15000)
                    time.sleep(2)
                except Exception:
                    pass
                
                for target in targets:
                    username = target["name"]
                    try:
                        log(f"Checking @{username}...")
                        
                        # 1. Profile metrics
                        profile_data = fetch_profile_metrics(page, username)
                        if not profile_data.get("ok"):
                            error = profile_data.get("error", "profile fetch failed")
                            log(f"  @{username}: profile failed: {error}", "WARNING")
                            save_snapshot(username, {}, profile_id, error)
                            result["errors"].append(f"{username}: {error}")
                            continue
                        
                        user_id = profile_data.get("user_id", "")
                        
                        # 2. Post metrics
                        post_data = fetch_post_metrics(page, user_id, count=12)
                        
                        # 3. Reels count
                        reels_data = fetch_reels_metrics(page, user_id)
                        
                        metrics = {
                            "followers": profile_data.get("followers", 0),
                            "following": profile_data.get("following", 0),
                            "posts_count": profile_data.get("posts_count", 0),
                            "total_likes": post_data.get("total_likes", 0),
                            "total_comments": post_data.get("total_comments", 0),
                            "total_views": post_data.get("total_views", 0),
                            "active_stories_count": reels_data.get("active_stories_count", 0),
                            "posts": post_data.get("posts", []),
                        }
                        
                        save_snapshot(username, metrics, profile_id)
                        result["checked"] += 1
                        log(f"  @{username}: ✅ followers={metrics['followers']}, views={metrics['total_views']}, likes={metrics['total_likes']}")
                        
                        time.sleep(random.uniform(2.0, 5.0))
                        
                    except Exception as e:
                        log(f"  @{username}: ERROR: {e}", "ERROR")
                        save_snapshot(username, {}, profile_id, str(e))
                        result["errors"].append(f"{username}: {e}")
                
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
                try:
                    p.stop()
                except Exception:
                    pass
        except Exception as e:
            log(f"Browser session error: {e}", "ERROR")
            result["errors"].append(f"browser: {e}")
        finally:
            result["done"] = True
    
    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=180)  # 3 min max
    
    return result["checked"], result["errors"]


def run_once() -> int:
    """One check cycle. Returns number of accounts successfully checked."""
    conn = _db_conn()
    try:
        ensure_metrics_schema(conn)

        # Get parser profiles from config or env
        profiles = ADS_POWER_PARSER_PROFILES
        if not profiles:
            profiles_raw = get_config(conn, "parser_profiles", "")
            profiles = [p.strip() for p in profiles_raw.split(",") if p.strip()]

        if not profiles:
            log("No parser profiles configured.", "WARNING")
            return 0

        targets = get_target_accounts(conn)
        if not targets:
            log("No target accounts to check.")
            return 0

        log(f"Checking {len(targets)} accounts using {len(profiles)} parser profile(s)")

        checked = 0
        for profile_id in profiles:
            if checked >= len(targets):
                break

            # Start Ads Power browser
            start_result = start_ads_browser(profile_id)
            if not start_result.get("ok"):
                log(f"Failed to start browser for profile {profile_id}: {start_result.get('error')}", "ERROR")
                continue

            ws_url = start_result.get("ws_url", "")
            try:
                c, errors = _run_browser_session(ws_url, targets, profile_id)
                checked += c
            except Exception as e:
                log(f"Browser session failed: {e}", "ERROR")
            finally:
                stop_ads_browser(profile_id)

        log(f"Cycle complete: {checked}/{len(targets)} accounts checked")
        return checked

    except Exception as e:
        log(f"Cycle error: {e}\n{traceback.format_exc()}", "ERROR")
        return 0
    finally:
        conn.close()


def _upload_in_progress() -> bool:
    """True when an upload/workflow job is currently running.

    The checker and the upload workers both spawn browsers, and on this
    16GB VPS they genuinely compete: 4 parallel Camoufox already produced
    a MemoryError with ~4.7GB free. Metrics are never urgent — deferring a
    cycle by one interval costs nothing, whereas starving an upload of
    memory mid-run costs a real publish. So uploads always win.
    """
    try:
        conn = _db_conn()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM ig_web_upload_jobs WHERE status='running'"
            ).fetchone()
        finally:
            conn.close()
        return int((row["n"] if row else 0) or 0) > 0
    except Exception:
        return False  # can't tell -> don't block metrics collection


def _minutes_since_last_check() -> float:
    """Minutes since the most recent metrics snapshot of any account.
    Returns a large number when nothing has ever been collected."""
    try:
        conn = _db_conn()
        try:
            ensure_metrics_schema(conn)
            row = conn.execute(
                "SELECT MAX(checked_at) AS last FROM account_metrics_snapshots"
            ).fetchone()
        finally:
            conn.close()
        last = str((row["last"] if row else "") or "").strip()
        if not last:
            return 1e9
        last_dt = datetime.strptime(last[:19], "%Y-%m-%d %H:%M:%S")
        return (datetime.now() - last_dt).total_seconds() / 60.0
    except Exception:
        return 1e9  # unknown -> behave as if overdue, never block collection


def _checker_loop() -> None:
    """Background loop with randomization."""
    log("Metrics checker started")
    # Don't collect immediately on startup if a cycle ran recently. The
    # software gets restarted often during development, and each restart
    # used to kick off a full collection run right away — repeatedly
    # hitting every account far more often than the intended ~1h cadence,
    # for no new data. Wait out the remainder of the interval instead.
    elapsed = _minutes_since_last_check()
    if elapsed < BASE_INTERVAL_MIN:
        wait_min = BASE_INTERVAL_MIN - elapsed
        log(f"Last check was {elapsed:.0f} min ago — waiting {wait_min:.0f} min before first cycle")
        time.sleep(wait_min * 60)
    while True:
        try:
            if not CHECKER_ENABLED:
                log("Checker disabled, skipping cycle")
            elif _upload_in_progress():
                log("Upload in progress — skipping this metrics cycle (uploads get memory priority)")
            else:
                run_once()
        except Exception as e:
            log(f"Loop error: {e}", "ERROR")

        # Random sleep 55-75 minutes
        sleep_min = random.uniform(BASE_INTERVAL_MIN, BASE_INTERVAL_MAX)
        sleep_sec = sleep_min * 60
        log(f"Sleeping {sleep_min:.1f} minutes until next cycle")
        time.sleep(sleep_sec)


# ─── Data Access for Dashboard ───────────────────────────────────────────────

def get_overview(conn: sqlite3.Connection, hours: int = 24) -> dict[str, Any]:
    """Get overview metrics for all accounts."""
    ensure_metrics_schema(conn)
    cutoff = datetime.now().strftime("%Y-%m-%d %H:%M:%S", )  # now
    # Get latest snapshot per account
    rows = conn.execute("""
        SELECT s.* FROM account_metrics_snapshots s
        INNER JOIN (
            SELECT account_name, MAX(checked_at) as max_checked
            FROM account_metrics_snapshots
            GROUP BY account_name
        ) latest ON s.account_name = latest.account_name AND s.checked_at = latest.max_checked
        ORDER BY s.account_name
    """).fetchall()

    # Get snapshot from ~24h ago for delta
    old_rows = conn.execute("""
        SELECT s.* FROM account_metrics_snapshots s
        INNER JOIN (
            SELECT account_name, MAX(checked_at) as max_checked
            FROM account_metrics_snapshots
            WHERE checked_at <= datetime('now', '-{} hours')
            GROUP BY account_name
        ) old ON s.account_name = old.account_name AND s.checked_at = old.max_checked
    """.format(hours), ()).fetchall()
    old_map = {r["account_name"]: dict(r) for r in old_rows}

    accounts = []
    total_followers = 0
    total_views = 0
    total_likes = 0
    total_comments = 0
    delta_followers = 0
    delta_views = 0
    delta_likes = 0
    delta_comments = 0

    for r in rows:
        d = dict(r)
        old = old_map.get(d["account_name"], {})
        cur_fol = int(d["followers"] or 0)
        cur_views = int(d["total_views"] or 0)
        cur_likes = int(d["total_likes"] or 0)
        cur_comments = int(d["total_comments"] or 0)
        old_fol = int(old.get("followers", 0) or 0)
        old_views = int(old.get("total_views", 0) or 0)
        old_likes = int(old.get("total_likes", 0) or 0)
        old_comments = int(old.get("total_comments", 0) or 0)

        # Delta: only compute when BOTH values are non-zero.
        # If current or previous is zero, the data is unreliable (collection
        # failure) — showing a fake -82K drop would be misleading.
        df = cur_fol - old_fol if (cur_fol > 0 and old_fol > 0) else 0
        dv = cur_views - old_views if (cur_views > 0 and old_views > 0) else 0
        dl = cur_likes - old_likes if (cur_likes > 0 and old_likes > 0) else 0
        dc = cur_comments - old_comments if (cur_comments > 0 and old_comments > 0) else 0

        accounts.append({
            "name": d["account_name"],
            "followers": d["followers"],
            "following": d["following"],
            "posts_count": d["posts_count"],
            "total_likes": d["total_likes"],
            "total_comments": d["total_comments"],
            "total_views": d["total_views"],
            "active_stories_count": d["active_stories_count"],
            "checked_at": d["checked_at"],
            "error": d["error"],
            "delta": {
                "followers": df,
                "views": dv,
                "likes": dl,
                "comments": dc,
            },
        })

        total_followers += cur_fol
        total_views += cur_views
        total_likes += cur_likes
        total_comments += cur_comments
        delta_followers += df
        delta_views += dv
        delta_likes += dl
        delta_comments += dc

    return {
        "total": {
            "followers": total_followers,
            "views": total_views,
            "likes": total_likes,
            "comments": total_comments,
        },
        "delta_24h": {
            "followers": delta_followers,
            "views": delta_views,
            "likes": delta_likes,
            "comments": delta_comments,
        },
        "accounts": accounts,
        "hours": hours,
    }


def get_account_history(conn: sqlite3.Connection, account_name: str, hours: int = 168) -> list[dict[str, Any]]:
    """Get time-series history for one account (default 7 days)."""
    ensure_metrics_schema(conn)
    rows = conn.execute("""
        SELECT * FROM account_metrics_snapshots
        WHERE account_name = ?
        AND checked_at >= datetime('now', '-{} hours')
        ORDER BY checked_at ASC
    """.format(hours), (account_name,)).fetchall()
    return [dict(r) for r in rows]


# ─── Entry Point ──────────────────────────────────────────────────────────────

def start_checker_thread() -> threading.Thread:
    """Start checker in background thread."""
    t = threading.Thread(target=_checker_loop, daemon=True, name="metrics-checker")
    t.start()
    return t


if __name__ == "__main__":
    # Run one cycle for testing
    checked = run_once()
    print(f"Checked {checked} accounts")
