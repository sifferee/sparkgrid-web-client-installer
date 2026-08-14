"""
SparkGrid Telegram Bot.

Client to SparkGrid API — manages uploads, stories, login, metrics.
Does NOT touch server code, browsers, or DB directly. Only HTTP API calls.

Commands:
  /start     — welcome + main menu (inline buttons)
  /status    — account overview
  /metrics    — metrics dashboard summary
  /upload    — start reel upload (inline keyboard)
  /stories   — post stories (inline keyboard)
  /login     — auto login (inline keyboard)
  /check     — run metrics checker now
  /session   — check sessions (check_login on all)
  /delete_banned — delete banned accounts
  /stop      — stop all processes
  /add_user  — (admin) add user by chat_id: /add_user 123456789
  /remove_user — (admin) remove user by chat_id
  /users     — (admin) list authorized users

Passive: checks for new story triggers every 10 min, sends hourly summary.
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ─── Config ───────────────────────────────────────────────────────────────────

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = int(os.environ.get("TELEGRAM_CHAT_ID") or 0)
API_URL = os.environ.get("SPARKGRID_API_URL", "http://127.0.0.1:8770")

if not BOT_TOKEN:
    print("ERROR: set TELEGRAM_BOT_TOKEN env var")
    sys.exit(1)

# ─── User Authorization ───────────────────────────────────────────────────────

DATA_DIR = Path(os.environ.get("SPARKGRID_DATA_DIR") or ".")
USERS_FILE = DATA_DIR / "telegram_users.json"


def load_users() -> dict:
    """Load authorized users from JSON. Returns {str(chat_id): {username, role, added_at}}."""
    try:
        if USERS_FILE.exists():
            return json.loads(USERS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"Failed to load users file: {e}")
    # First run — seed with admin from env
    if CHAT_ID:
        admin = {str(CHAT_ID): {"username": "admin", "role": "admin", "added_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}}
        save_users(admin)
        return admin
    return {}


def save_users(users: dict) -> None:
    """Save authorized users to JSON. Writes to a temp file and renames it
    into place — os.replace() is atomic on the same filesystem, so a crash
    or force-kill mid-write can never leave USERS_FILE half-written. A
    plain write_text() could get killed mid-write (e.g. by a forced
    Stop-Process from the launcher scripts) and truncate the file, which
    load_users() would then treat as corrupt and silently reseed to just
    the admin — quietly dropping every approved user."""
    try:
        USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = USERS_FILE.with_suffix(USERS_FILE.suffix + ".tmp")
        tmp_path.write_text(json.dumps(users, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, USERS_FILE)
    except Exception as e:
        logger.warning(f"Failed to save users file: {e}")


AUTHORIZED_USERS: dict = load_users()


def is_authorized(user_id: int) -> bool:
    return str(user_id) in AUTHORIZED_USERS


def is_admin(user_id: int) -> bool:
    entry = AUTHORIZED_USERS.get(str(user_id), {})
    return entry.get("role") == "admin"


def authorized_chat_ids() -> list[int]:
    """Return all authorized chat IDs (for broadcasting)."""
    return [int(k) for k in AUTHORIZED_USERS if k.isdigit()]


def add_user(chat_id: int, username: str, role: str = "user") -> None:
    AUTHORIZED_USERS[str(chat_id)] = {
        "username": username,
        "role": role,
        "added_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_users(AUTHORIZED_USERS)


def remove_user(chat_id: int) -> bool:
    key = str(chat_id)
    if key in AUTHORIZED_USERS and AUTHORIZED_USERS[key].get("role") != "admin":
        del AUTHORIZED_USERS[key]
        save_users(AUTHORIZED_USERS)
        return True
    return False

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("sparkgrid-bot")

# Also log to file
log_dir = os.path.join(os.environ.get("SPARKGRID_DATA_DIR", "."), "logs")
try:
    os.makedirs(log_dir, exist_ok=True)
    logger.addHandler(logging.FileHandler(os.path.join(log_dir, "telegram_bot.log"), encoding="utf-8"))
except Exception as _exc:
    logger.debug("%s: %s", type(_exc).__name__, _exc)
    pass

# ─── Action Logger ─────────────────────────────────────────────────────────────

ACTION_LOG_DIR = os.path.join(log_dir, "telegram_actions")
try:
    os.makedirs(ACTION_LOG_DIR, exist_ok=True)
except Exception as _exc:
    logger.debug("%s: %s", type(_exc).__name__, _exc)
    pass

def log_action(user_id, username, action, detail="", result="", error=""):
    """Log every Telegram action to a separate file per day."""
    ts = datetime.now()
    date_str = ts.strftime("%Y-%m-%d")
    log_file = os.path.join(ACTION_LOG_DIR, f"actions_{date_str}.log")
    line = f"[{ts.strftime('%H:%M:%S')}] user={username}({user_id}) action={action}"
    if detail:
        line += f" detail={detail}"
    if result:
        line += f" result={result}"
    if error:
        line += f" ERROR={error}"
    line += "\n"
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        pass

# ─── Helpers ──────────────────────────────────────────────────────────────────

def fmt(n):
    if n >= 1_000_000: return f"{n/1e6:.1f}M"
    if n >= 1_000: return f"{n/1e3:.1f}K"
    return str(n)

def fmt_delta(d):
    if d > 0: return f"+{fmt(d)}"
    if d < 0: return fmt(d)
    return "0"

def is_account_usable(a: dict) -> bool:
    """Check if account is logged_in AND not suspended (even hidden in error field)."""
    login = a.get("web_upload_login_status", "")
    if login != "logged_in":
        return False
    # Also check error field for hidden suspend/ban keywords
    error = str(a.get("web_upload_last_error") or "").lower()
    if any(w in error for w in ("suspend", "banned", "disabled", "restrict", "checkpoint", "challenge")):
        return False
    return True


def _is_banned(a: dict) -> bool:
    error = str(a.get("web_upload_last_error") or "").lower()
    return any(w in error for w in ("suspend", "banned", "disabled", "restrict", "checkpoint", "challenge"))


def _cooldown_hours_remaining(a: dict) -> float:
    """Hours remaining before this account clears its upload cooldown.
    0 or less = ready now.

    Alexander's explicit rule (2026-08-11): a brand-new account (never
    uploaded yet) gets a 6h cooldown measured from when it was added to
    the software (created_at) — not the normal 8h. After its first
    upload, the standard 8h cooldown applies, measured from
    web_upload_last_upload_at.

    Computed directly here from created_at/last_upload_at rather than
    trusting web_upload_cooldown_until — that column's write path lives
    in a module outside this repo (ig_signals.py) that hasn't been
    verified to implement this same 6h-for-new-accounts rule, so this
    is deliberately self-contained rather than assuming.

    If neither timestamp is available (legacy accounts added before
    created_at was populated) — don't block. Missing data isn't
    evidence of an active cooldown, and blocking on a guess would keep
    an account stuck in a synthetic cooldown forever.
    """
    last_upload = str(a.get("web_upload_last_upload_at") or "").strip()
    if last_upload:
        anchor, window_hours = last_upload, 8.0
    else:
        anchor, window_hours = str(a.get("created_at") or "").strip(), 6.0
    if not anchor:
        return 0.0
    try:
        anchor_dt = datetime.strptime(anchor[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return 0.0
    elapsed_hours = (datetime.now() - anchor_dt).total_seconds() / 3600.0
    return max(0.0, window_hours - elapsed_hours)


def _categorize_for_mass_upload(accounts: list) -> dict:
    """Bucket accounts for a mass-upload trigger: ready to go now, or the
    specific reason they're being skipped. Checked in a fixed priority
    order — an account only ever lands in ONE bucket, even if it
    technically matches more than one condition (e.g. banned AND in
    cooldown — banned is the more useful thing to tell the user)."""
    buckets = {"ready": [], "cooldown": [], "banned": [], "not_logged_in": [], "error": []}
    for a in accounts:
        if _is_banned(a):
            buckets["banned"].append(a)
            continue
        if str(a.get("web_upload_login_status") or "") != "logged_in":
            buckets["not_logged_in"].append(a)
            continue
        if _cooldown_hours_remaining(a) > 0:
            buckets["cooldown"].append(a)
            continue
        if str(a.get("web_upload_last_error") or "").strip():
            buckets["error"].append(a)
            continue
        buckets["ready"].append(a)
    return buckets

# ─── API Client ───────────────────────────────────────────────────────────────
# post-story endpoint reads form data, not JSON.
# Other endpoints (start, workflow, stop, metrics/run, delete-banned) accept JSON.

async def api_get(session, path):
    """GET request with one transparent retry on transient network errors.

    Diagnosed 2026-08-12: a single occurrence where the backend was
    genuinely busy (many concurrent dashboard requests at once) caused a
    ~26s delay that came close to the 30s timeout, ending in a
    connection reset. The server was NOT down — it answered a request 4s
    before and 1s after this one failed. This was a brief contention
    blip, not an outage.

    GET requests are safe to retry blindly (read-only, no side effects),
    so one retry after a short pause turns a rare transient blip into a
    non-event for the user instead of a scary "SparkGrid недоступен".

    api_post_json is deliberately NOT given this treatment — retrying a
    state-changing request risks a duplicate action (e.g. a second
    upload job) if the first attempt actually succeeded but its response
    was what got lost, not the request itself.
    """
    for attempt in (1, 2):
        try:
            async with session.get(f"{API_URL}{path}", timeout=aiohttp.ClientTimeout(total=30)) as resp:
                return await resp.json()
        except Exception as e:
            logger.debug("%s: %s (attempt %d/2)", type(e).__name__, e, attempt)
            if attempt == 1:
                await asyncio.sleep(1.5)
                continue
            return {"ok": False, "error": str(e)}

async def api_post_json(session, path, body=None):
    """POST JSON body. Used by /start, /workflow, /stop, /metrics/run, /delete-banned."""
    try:
        async with session.post(
            f"{API_URL}{path}",
            json=body or {},
            timeout=aiohttp.ClientTimeout(total=120),
        ) as resp:
            return await resp.json()
    except Exception as e:
        logger.debug("%s: %s", type(e).__name__, e)
        return {"ok": False, "error": str(e)}

async def api_post_form(session, path, form_fields=None):
    """POST form data. Used by /post-story endpoint which reads request.form()."""
    try:
        data = aiohttp.FormData()
        for key, value in (form_fields or {}).items():
            if isinstance(value, list):
                value = ",".join(str(v) for v in value)
            data.add_field(key, str(value))
        async with session.post(
            f"{API_URL}{path}",
            data=data,
            timeout=aiohttp.ClientTimeout(total=120),
        ) as resp:
            return await resp.json()
    except Exception as e:
        logger.debug("%s: %s", type(e).__name__, e)
        return {"ok": False, "error": str(e)}

# Keep backward-compat alias
async def api_post(session, path, body=None):
    return await api_post_json(session, path, body)

# ─── Main Menu ─────────────────────────────────────────────────────────────────

def main_menu_keyboard():
    """Inline keyboard for /start command — replaces typed commands."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Метрики", callback_data="cmd:metrics"),
         InlineKeyboardButton("📋 Статус", callback_data="cmd:status")],
        [InlineKeyboardButton("🚀 Залить рилсы", callback_data="cmd:upload"),
         InlineKeyboardButton("📸 Истории", callback_data="cmd:stories")],
        [InlineKeyboardButton("🔍 Проверить сессии", callback_data="cmd:session"),
         InlineKeyboardButton("🔐 Логин", callback_data="cmd:login")],
        [InlineKeyboardButton("📈 Собрать метрики", callback_data="cmd:check"),
         InlineKeyboardButton("🗑 Удалить забаненные", callback_data="cmd:delete_banned")],
        [InlineKeyboardButton("🛑 Стоп всё", callback_data="cmd:stop")],
    ])

WELCOME = """🤖 SparkGrid Бот

Меню всегда доступно по кнопке меню (≡) слева от поля ввода.

Или команды:
/status — список аккаунтов
/metrics — метрика
/upload — залив рилсов
/stories — истории
/login — авто-логин
/session — проверка сессий
/check — сбор метрик
/delete_banned — удалить забаненные
/stop — стоп"""

# ─── Commands ──────────────────────────────────────────────────────────────────

async def _check_auth(update: Update) -> bool:
    """Check if user is authorized. If not, send access request to admins."""
    user = update.effective_user
    if user and is_authorized(user.id):
        return True
    # Unauthorized — notify admins
    if user:
        msg = f"🔒 Запрос доступа:\n@{user.username or '?'} ({user.id})\nИмя: {user.first_name or '?'}"
        for admin_id in authorized_chat_ids():
            if is_admin(admin_id):
                try:
                    keyboard = InlineKeyboardMarkup([[
                        InlineKeyboardButton("✅ Одобрить", callback_data=f"approve:{user.id}:{user.username or user.first_name or '?'}"),
                        InlineKeyboardButton("❌ Отклонить", callback_data=f"deny:{user.id}"),
                    ]])
                    await update.get_bot().send_message(chat_id=admin_id, text=msg, reply_markup=keyboard)
                except Exception as _exc:
                    logger.debug("%s: %s", type(_exc).__name__, _exc)
                    pass
    if update.message:
        await update.message.reply_text("🔒 У вас нет доступа. Запрос отправлен администратору.")
    return False


async def cmd_start(update: Update, ctx):
    if not await _check_auth(update):
        return
    log_action(update.effective_user.id, update.effective_user.username, "start")
    await update.message.reply_text(WELCOME, reply_markup=main_menu_keyboard())

async def cmd_status(update: Update, ctx):
    if not await _check_auth(update):
        return
    log_action(update.effective_user.id, update.effective_user.username, "status")
    async with aiohttp.ClientSession() as session:
        data = await api_get(session, "/api/ig-web-upload/overview")
    if not data.get("ok"):
        log_action(update.effective_user.id, update.effective_user.username, "status", error="SparkGrid недоступен")
        await _reply(update, "❌ SparkGrid недоступен")
        return
    accounts = data.get("accounts", [])
    if not accounts:
        await _reply(update, "Нет аккаунтов")
        return

    active = [a for a in accounts if is_account_usable(a)]
    suspended = [a for a in accounts if a.get("web_upload_login_status") == "suspended"]
    other = [a for a in accounts if a not in active and a not in suspended]

    msg = f"📋 Аккаунты: {len(accounts)} всего\n"
    msg += f"✅ Активные: {len(active)}\n"
    if suspended:
        msg += f"❌ Забаненные: {len(suspended)}\n"
    if other:
        msg += f"⚠️ Проблемные: {len(other)}\n"

    # Show active accounts (max 10 if few, compact if many)
    if len(active) <= 10:
        msg += "\n"
        for a in active:
            priv = a.get("web_privacy_status", "?")
            msg += f"✅ @{a['name']} | {priv}\n"
    else:
        # Compact: just names
        msg += "\n✅ " + ", ".join(f"@{a['name']}" for a in active[:15])
        if len(active) > 15:
            msg += f" +{len(active)-15} ещё"

    if suspended and len(suspended) <= 5:
        msg += "\n\n"
        for a in suspended:
            msg += f"❌ @{a['name']}\n"

    log_action(update.effective_user.id, update.effective_user.username, "status", result=f"{len(accounts)} accounts")
    await _reply(update, msg)

async def cmd_metrics(update: Update, ctx):
    if not await _check_auth(update):
        return
    log_action(update.effective_user.id, update.effective_user.username, "metrics")
    await _send_metrics(update)

async def _send_metrics(update_or_query):
    async with aiohttp.ClientSession() as session:
        data = await api_get(session, "/api/ig-web-upload/metrics/overview?hours=24")
    if not data.get("ok"):
        log_action(0, "?", "metrics", error="нет данных")
        await _reply(update_or_query, "❌ Нет данных метрики. Запусти /check")
        return

    t = data.get("total", {})
    dl = data.get("delta_24h", {})
    accounts = data.get("accounts", [])

    def _arrow(delta):
        """Format delta with arrow emoji — only show if non-zero."""
        if delta > 0:
            return f" ▲{fmt(delta)}"
        elif delta < 0:
            return f" ▼{abs(delta)}"
        return ""

    # ── Summary block (always shown, compact) ──
    msg = "📊 Метрики за 24ч\n"
    # Say plainly when the collector itself is down. Without this the
    # numbers simply stop changing and look like a fleet that isn't
    # growing — Alexander lost a day to AdsPower being closed while every
    # cycle quietly logged "0 accounts checked".
    collector_error = str(data.get("collector_error") or "").strip()
    if collector_error:
        when = str(data.get("collector_error_at") or "")[:16]
        msg = f"⚠️ Сбор метрик не работает: {collector_error}\n"
        if when:
            msg += f"   (последняя попытка: {when})\n"
        msg += "   Данные ниже устарели.\n\n📊 Метрики за 24ч\n"
    msg += f"Подписчики: {fmt(t.get('followers',0))}{_arrow(dl.get('followers',0))}\n"
    msg += f"Просмотры: {fmt(t.get('views',0))}{_arrow(dl.get('views',0))}\n"
    msg += f"Лайки: {fmt(t.get('likes',0))}{_arrow(dl.get('likes',0))}\n"
    msg += f"Комментарии: {fmt(t.get('comments',0))}{_arrow(dl.get('comments',0))}\n"

    if not accounts:
        await _reply(update_or_query, msg)
        return

    # ── Top performers (max 5, sorted by followers) ──
    with_data = [a for a in accounts if int(a.get("followers", 0) or 0) > 0 or int(a.get("total_views", 0) or 0) > 0]
    no_data = [a for a in accounts if int(a.get("followers", 0) or 0) == 0 and int(a.get("total_views", 0) or 0) == 0]

    if with_data:
        # Sort by followers desc, take top 5
        top = sorted(with_data, key=lambda a: int(a.get("followers", 0) or 0), reverse=True)[:5]
        msg += f"\n🏆 Топ-{len(top)} по подписчикам:"
        for a in top:
            name = a.get("name", "?")
            fol = int(a.get("followers", 0) or 0)
            views = int(a.get("total_views", 0) or 0)
            delta = a.get("delta", {})
            d_fol = _arrow(delta.get("followers", 0))
            msg += f"\n  {fmt(fol)} подп | {fmt(views)} просм @{name}{d_fol}"

    # ── Accounts showing all zeros (one line) ──
    # Split by reason. A banned account having zeros is expected — nothing
    # to collect — while a logged-in account with zeros is either brand new
    # or a real collection failure. Previously everything was lumped into
    # one alarming "N accounts without data" count that never went down.
    if no_data:
        def _acct_banned(a):
            status = str(a.get("login_status") or "")
            err = str(a.get("account_error") or "").lower()
            return status == "suspended" or any(
                w in err for w in ("suspend", "banned", "disabled", "restrict")
            )

        banned_zero = [a for a in no_data if _acct_banned(a)]
        real_zero = [a for a in no_data if not _acct_banned(a)]
        if real_zero:
            if len(real_zero) <= 3:
                names = ", ".join(f"@{a.get('name','?')}" for a in real_zero)
                msg += f"\n\n⚠️ Без данных: {names}"
            else:
                msg += f"\n\n⚠️ {len(real_zero)} аккаунтов без данных (новые или сбой сбора)"
        if banned_zero:
            msg += f"\n🚫 {len(banned_zero)} забаненных — метрики не собираются"

    # ── Failed accounts (restricted/banned) ──
    failed = [a for a in accounts if a.get("error")]
    if failed and len(failed) <= 3:
        for a in failed:
            if a not in no_data:
                msg += f"\n🚫 @{a.get('name','?')}: {str(a.get('error',''))[:40]}"

    log_action(0, "?", "metrics", result=f"followers={t.get('followers',0)} accounts={len(accounts)}")
    await _reply(update_or_query, msg)

async def cmd_upload(update: Update, ctx):
    if not await _check_auth(update):
        return
    log_action(update.effective_user.id, update.effective_user.username, "upload_menu")
    await _show_upload_menu(update)

async def _show_upload_menu(update_or_query):
    async with aiohttp.ClientSession() as session:
        data = await api_get(session, "/api/ig-web-upload/overview")
    if not data.get("ok"):
        await _reply(update_or_query, "❌ SparkGrid недоступен")
        return
    accounts = [a for a in data.get("accounts", []) if is_account_usable(a)]
    if not accounts:
        await _reply(update_or_query, "Нет готовых аккаунтов (logged_in)")
        return
    keyboard = []
    keyboard.append([InlineKeyboardButton("🚀 Все ready", callback_data="upload_all")])
    for a in accounts:
        keyboard.append([InlineKeyboardButton(f"@{a['name']}", callback_data=f"upload:{a['name']}")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="cmd:start")])
    await _reply(update_or_query, "Выбери аккаунты для залива:", reply_markup=InlineKeyboardMarkup(keyboard))

async def cmd_stories(update: Update, ctx):
    if not await _check_auth(update):
        return
    log_action(update.effective_user.id, update.effective_user.username, "stories_menu")
    await _show_stories_menu(update)

async def _show_stories_menu(update_or_query):
    async with aiohttp.ClientSession() as session:
        data = await api_get(session, "/api/ig-web-upload/overview")
    if not data.get("ok"):
        await _reply(update_or_query, "❌ SparkGrid недоступен")
        return
    accounts = [a for a in data.get("accounts", []) if is_account_usable(a)]
    if not accounts:
        await _reply(update_or_query, "Нет готовых аккаунтов")
        return
    keyboard = []
    keyboard.append([InlineKeyboardButton("📸 Все", callback_data="stories_all")])
    for a in accounts:
        keyboard.append([InlineKeyboardButton(f"@{a['name']}", callback_data=f"stories:{a['name']}")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="cmd:start")])
    await _reply(update_or_query, "Выбери аккаунты для историй:", reply_markup=InlineKeyboardMarkup(keyboard))

async def cmd_login(update: Update, ctx):
    if not await _check_auth(update):
        return
    log_action(update.effective_user.id, update.effective_user.username, "login_menu")
    async with aiohttp.ClientSession() as session:
        data = await api_get(session, "/api/ig-web-upload/overview")
    if not data.get("ok"):
        await _reply(update, "❌ SparkGrid недоступен")
        return
    accounts = data.get("accounts", [])
    if not accounts:
        await _reply(update, "Нет аккаунтов")
        return
    keyboard = []
    keyboard.append([InlineKeyboardButton("🔐 Все", callback_data="login_all")])
    for a in accounts:
        keyboard.append([InlineKeyboardButton(f"@{a['name']}", callback_data=f"login:{a['name']}")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="cmd:start")])
    await _reply(update, "Выбери аккаунты для авто-логина:", reply_markup=InlineKeyboardMarkup(keyboard))

async def cmd_check(update: Update, ctx):
    if not await _check_auth(update):
        return
    log_action(update.effective_user.id, update.effective_user.username, "check_metrics")
    async with aiohttp.ClientSession() as session:
        data = await api_post_json(session, "/api/ig-web-upload/metrics/run")
    if data.get("ok"):
        await _reply(update, "✅ Проверка метрик запущена. Результаты через ~2 мин.")
    else:
        log_action(update.effective_user.id, update.effective_user.username, "check_metrics", error=str(data.get("error")))
        await _reply(update, f"❌ {data.get('error', 'ошибка')}")

async def cmd_session(update: Update, ctx):
    if not await _check_auth(update):
        return
    log_action(update.effective_user.id, update.effective_user.username, "session_check")
    await _run_session_check(update)

async def _run_session_check(update_or_query):
    async with aiohttp.ClientSession() as session:
        overview = await api_get(session, "/api/ig-web-upload/overview")
    if not overview.get("ok"):
        await _reply(update_or_query, "❌ SparkGrid недоступен")
        return
    accounts = overview.get("accounts", [])
    if not accounts:
        await _reply(update_or_query, "Нет аккаунтов")
        return

    await _reply(update_or_query, f"🔍 Проверяю сессии для {len(accounts)} аккаунтов...")

    async with aiohttp.ClientSession() as session:
        result = await api_post_json(session, "/api/ig-web-upload/workflow", {"task": "check_login", "accounts": [a["name"] for a in accounts]})

    if not result.get("ok"):
        await _reply(update_or_query, f"❌ Ошибка запуска: {result.get('error', 'ошибка')}")
        return

    # Wait for workflow to complete (check every 15s, max 5 min)
    await asyncio.sleep(15)
    for _ in range(19):
        async with aiohttp.ClientSession() as session:
            health = await api_get(session, "/api/health")
        if health.get("ok") and not health.get("process", {}).get("running", False):
            break
        await asyncio.sleep(15)

    async with aiohttp.ClientSession() as session:
        data = await api_get(session, "/api/ig-web-upload/overview")
    if not data.get("ok"):
        await _reply(update_or_query, "❌ Не удалось получить результаты")
        return
    accounts = data.get("accounts", [])

    active = []
    expired = []
    other = []
    for a in accounts:
        status = a.get("web_upload_login_status", "unknown")
        name = a.get("name", "?")
        # Show when the CHECK ran, not when credentials were last entered.
        # "last: 2026-08-10" next to a fresh check result read as "this
        # result is 4 days old", when in fact it was the login date — a
        # session check verifies an existing session and never re-logs in,
        # so that date legitimately stays put for weeks.
        checked = a.get("web_upload_session_checked_at", "")
        if status == "logged_in" and is_account_usable(a):
            active.append(f"✅ @{name} (проверен: {checked[:16] if checked else '—'})")
        elif status in ("suspended", "incorrect_credentials", "consent_failed", "manual_required", "browser_internal_error"):
            expired.append(f"❌ @{name} — {status}")
        else:
            other.append(f"⚠️ @{name} — {status}")
    msg = "*Результат проверки сессий*\n"
    if active:
        msg += f"\n🟢 Активные ({len(active)}):\n" + "\n".join(active[:15])
        if len(active) > 15:
            msg += f"\n+{len(active) - 15} ещё"
        msg += "\n"
    if expired:
        msg += f"\n🔴 Протухшие/Заблокированные ({len(expired)}):\n" + "\n".join(expired[:15])
        if len(expired) > 15:
            msg += f"\n+{len(expired) - 15} ещё"
        msg += "\n"
    if other:
        msg += f"\n🟡 Другие ({len(other)}):\n" + "\n".join(other[:5])
        if len(other) > 5:
            msg += f"\n+{len(other) - 5} ещё"
        msg += "\n"

    keyboard = []
    if expired:
        expired_names = [a["name"] for a in accounts if a.get("web_upload_login_status") in ("suspended", "incorrect_credentials", "consent_failed", "manual_required", "browser_internal_error")]
        keyboard.append([InlineKeyboardButton(f"🔐 Перезалогинить {len(expired_names)} протухших", callback_data="login_expired")])
        suspended_names = [a["name"] for a in accounts if a.get("web_upload_login_status") in ("suspended",)]
        if suspended_names:
            keyboard.append([InlineKeyboardButton(f"🗑 Удалить {len(suspended_names)} забаненных", callback_data="confirm_delete_banned")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="cmd:start")])
    await _reply(update_or_query, msg, reply_markup=InlineKeyboardMarkup(keyboard))
    log_action(0, "?", "session_check", result=f"active={len(active)} expired={len(expired)}")

async def cmd_stop(update: Update, ctx):
    if not await _check_auth(update):
        return
    log_action(update.effective_user.id, update.effective_user.username, "stop")
    async with aiohttp.ClientSession() as session:
        data = await api_post_json(session, "/api/ig-web-upload/stop")
    if data.get("ok"):
        await _reply(update, "🛑 Все процессы остановлены")
    else:
        log_action(update.effective_user.id, update.effective_user.username, "stop", error=str(data.get("error")))
        await _reply(update, f"❌ {data.get('error', 'ошибка')}")

async def cmd_delete_banned(update: Update, ctx):
    """Delete all suspended/banned accounts."""
    if not await _check_auth(update):
        return
    log_action(update.effective_user.id, update.effective_user.username, "delete_banned")
    await _show_delete_banned(update)

async def _show_delete_banned(update_or_query):
    async with aiohttp.ClientSession() as session:
        data = await api_get(session, "/api/ig-web-upload/accounts/banned")
    if not data.get("ok"):
        await _reply(update_or_query, "❌ SparkGrid недоступен")
        return
    # API returns {"accounts": ["name1", "name2"]} — list[str], not list[dict]
    banned = data.get("accounts", [])
    if not banned:
        await _reply(update_or_query, "✅ Нет забаненных аккаунтов")
        return
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🗑 Удалить {len(banned)} забаненных", callback_data="confirm_delete_banned")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="cmd:start")],
    ])
    names = "\n".join([f"❌ @{n}" for n in banned[:15]])
    await _reply(update_or_query, f"Найдено {len(banned)} забаненных аккаунтов:\n\n{names}", reply_markup=keyboard)

# ─── Unified reply helper ──────────────────────────────────────────────────────

async def _reply(update_or_query, text, **kwargs):
    """Reply via message (command) or edit+send (callback query)."""
    if hasattr(update_or_query, "edit_message_text"):
        try:
            await update_or_query.edit_message_text(text, **kwargs)
            return
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            pass  # message not modified or too long → fall through to send new
    if hasattr(update_or_query, "message") and update_or_query.message:
        await update_or_query.message.reply_text(text, **kwargs)
    elif hasattr(update_or_query, "reply_text"):
        await update_or_query.reply_text(text, **kwargs)

# ─── Admin Commands ────────────────────────────────────────────────────────────

async def cmd_add_user(update: Update, ctx):
    """Admin: /add_user <chat_id> [username] — add user to whitelist."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Только администратор может добавлять пользователей")
        return
    args = ctx.args
    if not args:
        await update.message.reply_text("Использование: /add_user <chat_id> [username]\nНапример: /add_user 123456789 daris")
        return
    try:
        new_chat_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ chat_id должен быть числом")
        return
    username = args[1] if len(args) > 1 else "user"
    add_user(new_chat_id, username, role="user")
    log_action(update.effective_user.id, update.effective_user.username, "add_user", detail=f"new_user={new_chat_id}({username})")
    await update.message.reply_text(f"✅ Пользователь @{username} ({new_chat_id}) добавлен")


async def cmd_remove_user(update: Update, ctx):
    """Admin: /remove_user <chat_id> — remove user from whitelist."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Только администратор может удалять пользователей")
        return
    args = ctx.args
    if not args:
        await update.message.reply_text("Использование: /remove_user <chat_id>")
        return
    try:
        target_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ chat_id должен быть числом")
        return
    if is_admin(target_id):
        await update.message.reply_text("⛔ Нельзя удалить администратора")
        return
    if remove_user(target_id):
        log_action(update.effective_user.id, update.effective_user.username, "remove_user", detail=f"removed={target_id}")
        await update.message.reply_text(f"✅ Пользователь {target_id} удалён")
    else:
        await update.message.reply_text("❌ Пользователь не найден")


async def cmd_users(update: Update, ctx):
    """Admin: list all authorized users."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Только администратор")
        return
    if not AUTHORIZED_USERS:
        await update.message.reply_text("Нет авторизованных пользователей")
        return
    lines = ["👥 Авторизованные пользователи:"]
    for chat_id, info in AUTHORIZED_USERS.items():
        role_emoji = "👑" if info.get("role") == "admin" else "👤"
        lines.append(f"{role_emoji} @{info.get('username','?')} ({chat_id}) — {info.get('role','user')}")
    await update.message.reply_text("\n".join(lines))


# ─── Callback Handler ─────────────────────────────────────────────────────────

async def callback_handler(update: Update, ctx):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id if update.effective_user else 0
    username = update.effective_user.username if update.effective_user else "?"

    # ─── Access approval/denial (admin only) ───
    if data.startswith("approve:"):
        if not is_admin(user_id):
            await query.edit_message_text("⛔ Только администратор может одобрять")
            return
        parts = data.split(":", 2)
        if len(parts) < 3:
            await query.edit_message_text("❌ Неверный формат")
            return
        approved_id = int(parts[1])
        approved_name = parts[2]
        add_user(approved_id, approved_name, role="user")
        log_action(user_id, username, "approve_user", detail=f"approved={approved_id}({approved_name})")
        await query.edit_message_text(f"✅ Одобрен: @{approved_name} ({approved_id})")
        # Notify the approved user
        try:
            await ctx.bot.send_message(chat_id=approved_id, text="✅ Доступ одобрен! Напиши /start чтобы начать.")
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            pass
        return

    if data.startswith("deny:"):
        if not is_admin(user_id):
            await query.edit_message_text("⛔ Только администратор")
            return
        denied_id = int(data.split(":")[1])
        log_action(user_id, username, "deny_user", detail=f"denied={denied_id}")
        await query.edit_message_text(f"❌ Отклонён: {denied_id}")
        return

    # ─── Vision-fallback suggestion approval (admin only) ───
    # Approval never applies code automatically — it only formats a
    # ready-to-relay Hermes task, same as every other fix this session.
    # A human still reviews and applies it via the normal Hermes flow.
    if data.startswith("vfapprove:") or data.startswith("vfdeny:"):
        if not is_admin(user_id):
            await query.edit_message_text("⛔ Только администратор")
            return
        record_id = data.split(":", 1)[1]
        try:
            import vision_review
            records = vision_review._load_records()
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            records = []
        record = next((r for r in records if r.get("id") == record_id), None)

        if data.startswith("vfdeny:"):
            log_action(user_id, username, "vf_deny", detail=record_id)
            await query.edit_message_text("❌ Предложение отклонено, ничего не меняем.")
            return

        if not record or not record.get("proposal"):
            await query.edit_message_text("❌ Запись не найдена (возможно, лог уже очищен).")
            return

        log_action(user_id, username, "vf_approve", detail=record_id)
        task_text = (
            "ЗАДАЧА: применить предложение зрения к быстрому пути (не менять "
            "поведение, только добавить распознавание паттерна).\n\n"
            f"Контекст: зрение сработало для \"{record.get('intent')}\" — "
            "структурный поиск не нашёл элемент сам.\n\n"
            f"Предложение Sonnet:\n{record.get('proposal')}\n\n"
            "Найди соответствующий regex/паттерн в коде (скорее всего "
            "blocking_popup_transaction.py или рядом) и добавь предложенный "
            "вариант текста, НЕ меняя остальную логику. Проверь py_compile "
            "и pytest tests/test_js_syntax.py перед коммитом."
        )
        await query.edit_message_text(
            f"✅ Одобрено. Задача для Гермеса:\n\n{task_text}"
        )
        return

    # ─── Auth check for all other callbacks ───
    if not is_authorized(user_id):
        await query.edit_message_text("🔒 Нет доступа")
        return

    if data == "cancel":
        log_action(user_id, username, "cancel")
        await query.edit_message_text("Отменено")
        return

    # ─── Menu navigation (cmd:* callbacks) ───
    if data == "cmd:start":
        await query.edit_message_text(WELCOME, reply_markup=main_menu_keyboard())
        return
    if data == "cmd:status":
        await cmd_status(update, ctx)
        return
    if data == "cmd:metrics":
        await _send_metrics(query)
        return
    if data == "cmd:upload":
        await _show_upload_menu(query)
        return
    if data == "cmd:stories":
        await _show_stories_menu(query)
        return
    if data == "cmd:session":
        await _run_session_check(query)
        return
    if data == "cmd:login":
        await cmd_login(update, ctx)
        return
    if data == "cmd:check":
        log_action(user_id, username, "check_metrics")
        async with aiohttp.ClientSession() as session:
            result = await api_post_json(session, "/api/ig-web-upload/metrics/run")
        if result.get("ok"):
            await query.message.reply_text("✅ Проверка метрик запущена. Результаты через ~2 мин.")
        else:
            await query.message.reply_text(f"❌ {result.get('error', 'ошибка')}")
        return
    if data == "cmd:delete_banned":
        await _show_delete_banned(query)
        return
    if data == "cmd:stop":
        log_action(user_id, username, "stop")
        async with aiohttp.ClientSession() as session:
            result = await api_post_json(session, "/api/ig-web-upload/stop")
        if result.get("ok"):
            await query.message.reply_text("🛑 Все процессы остановлены")
        else:
            await query.message.reply_text(f"❌ {result.get('error', 'ошибка')}")
        return

    # ─── Action callbacks ───
    async with aiohttp.ClientSession() as session:
        if data.startswith("upload:"):
            name = data.split(":", 1)[1]
            await query.edit_message_text(f"🚀 Залив @{name}...")
            result = await api_post_json(session, "/api/ig-web-upload/start", {
                "accounts": [name], "engine": "clean_web", "browser_parallel": 1,
                # Warmup on/off is a Settings-page toggle now (Александр's
                # request 12.08) — deliberately NOT passing pre_warmup_*/
                # post_warmup_* here, so the backend applies whatever the
                # UI switch is currently set to, rather than the bot
                # silently overriding it with its own hardcoded choice.
                "target": 3, "cooldown_hours": 8,
            })
        elif data == "upload_all":
            overview = await api_get(session, "/api/ig-web-upload/overview")
            all_accounts = overview.get("accounts", [])
            buckets = _categorize_for_mass_upload(all_accounts)
            ready_names = [a["name"] for a in buckets["ready"]]

            skip_lines = []
            if buckets["cooldown"]:
                skip_lines.append(f"{len(buckets['cooldown'])} — кулдаун")
            if buckets["banned"]:
                skip_lines.append(f"{len(buckets['banned'])} — бан")
            if buckets["not_logged_in"]:
                skip_lines.append(f"{len(buckets['not_logged_in'])} — не залогинены")
            if buckets["error"]:
                skip_lines.append(f"{len(buckets['error'])} — ошибка")

            if not ready_names:
                summary = (
                    f"{len(all_accounts)} аккаунтов\n"
                    f"Готовых к заливке: 0\n\n"
                    f"Не залито:\n" + "\n".join(skip_lines) if skip_lines else "Нет аккаунтов вообще."
                )
                await query.edit_message_text(summary)
                log_action(user_id, username, "upload_all", detail="0 ready")
                return

            result = await api_post_json(session, "/api/ig-web-upload/start", {
                "accounts": ready_names, "engine": "clean_web", "browser_parallel": 5,
                # Warmup on/off — see note in the upload: branch above,
                # same reasoning applies here.
                "target": 3, "cooldown_hours": 8,
            })

            summary_lines = [
                f"{len(all_accounts)} аккаунтов",
                f"Запущено: {len(ready_names)}",
            ]
            if skip_lines:
                summary_lines.append("")
                summary_lines.append("Не залито:")
                summary_lines.extend(skip_lines)
            summary = "\n".join(summary_lines)

            if result.get("ok"):
                run_id = result.get("run_id", "")
                log_action(user_id, username, "upload_all", detail=f"ready={len(ready_names)}", result=f"run_id={run_id}")
                await query.edit_message_text(summary, reply_markup=main_menu_keyboard())
            else:
                log_action(user_id, username, "upload_all", error=str(result.get("error")))
                await query.edit_message_text(f"❌ {result.get('error', 'ошибка')}\n\n{summary}", reply_markup=main_menu_keyboard())
            return
        elif data.startswith("stories:"):
            name = data.split(":", 1)[1]
            await query.edit_message_text(f"📸 История @{name}...")
            # post-story endpoint reads form data, not JSON
            result = await api_post_form(session, "/api/ig-web-upload/post-story", {"accounts": name})
        elif data == "stories_all":
            await query.edit_message_text("📸 Истории на всех...")
            overview = await api_get(session, "/api/ig-web-upload/overview")
            names = [a["name"] for a in overview.get("accounts", []) if is_account_usable(a)]
            # post-story endpoint reads form data; pass as comma-separated string
            result = await api_post_form(session, "/api/ig-web-upload/post-story", {"accounts": ",".join(names)})
        elif data.startswith("login:"):
            name = data.split(":", 1)[1]
            await query.edit_message_text(f"🔐 Логин @{name}...")
            result = await api_post_json(session, "/api/ig-web-upload/workflow", {"task": "auto_login", "accounts": [name]})
        elif data == "login_all":
            await query.edit_message_text("🔐 Логин всех...")
            overview = await api_get(session, "/api/ig-web-upload/overview")
            names = [a["name"] for a in overview.get("accounts", [])]
            result = await api_post_json(session, "/api/ig-web-upload/workflow", {"task": "auto_login", "accounts": names})
        elif data == "login_expired":
            await query.edit_message_text("🔐 Перезалогинить протухших...")
            overview = await api_get(session, "/api/ig-web-upload/overview")
            names = [a["name"] for a in overview.get("accounts", []) if a.get("web_upload_login_status") in ("incorrect_credentials", "consent_failed", "manual_required", "suspended", "browser_internal_error", "unknown", "")]
            if not names:
                await query.message.reply_text("Нет протухших аккаунтов")
                return
            result = await api_post_json(session, "/api/ig-web-upload/workflow", {"task": "auto_login", "accounts": names})
        elif data == "delete_banned":
            await _show_delete_banned(query)
            return
        elif data == "confirm_delete_banned":
            await query.edit_message_text("🗑 Удаляю забаненные аккаунты...")
            result = await api_post_json(session, "/api/ig-web-upload/accounts/delete-banned")
            if result.get("ok"):
                deleted = result.get("deleted", 0)
                proxies = result.get("proxies_deleted", 0)
                msg = f"✅ Удалено {deleted} аккаунтов"
                if proxies:
                    msg += f", {proxies} прокси"
                await query.message.reply_text(msg, reply_markup=main_menu_keyboard())
            else:
                await query.message.reply_text(f"❌ {result.get('error', 'ошибка')}")
            log_action(user_id, username, "delete_banned", result=str(result))
            return
        else:
            return

    # Handle result for upload/stories/login callbacks
    if result.get("ok") and result.get("started", True) is not False:
        run_id = result.get("run_id", "")
        log_action(user_id, username, data, detail=f"accounts={result.get('accounts','')}", result=f"run_id={run_id}")
        await query.message.reply_text(f"✅ Запущено! run_id={run_id}", reply_markup=main_menu_keyboard())
    elif result.get("ok") and result.get("started", True) is False:
        # empty_selection or similar — ok=True but started=False
        reason = result.get("reason") or result.get("message") or "не запущено"
        log_action(user_id, username, data, error=reason)
        await query.message.reply_text(f"⚠️ {reason}", reply_markup=main_menu_keyboard())
    else:
        log_action(user_id, username, data, error=str(result.get("error")))
        await query.message.reply_text(f"❌ {result.get('error', 'ошибка')}", reply_markup=main_menu_keyboard())

# ─── Background: Story Trigger Notifications ──────────────────────────────────

last_seen_trigger_id = 0

async def background_check_triggers(ctx: ContextTypes.DEFAULT_TYPE):
    """Check for new story triggers every 10 min. Notify all authorized users."""
    global last_seen_trigger_id
    try:
        async with aiohttp.ClientSession() as session:
            data = await api_get(session, "/api/ig-web-upload/story-trigger/status")
        if not data.get("ok"):
            return
        triggers = data.get("triggers", [])
        for t in triggers:
            tid = int(t.get("id", 0))
            if tid <= last_seen_trigger_id:
                continue
            if t.get("story_posted_at") and t.get("trigger_views", 0) > 0:
                name = t.get("account_name", "?")
                views = t.get("trigger_views", 0)
                msg = f"🔔 @{name}: Рилс набрал {fmt(views)} просмотров — История залита автоматически!"
                for chat_id in authorized_chat_ids():
                    try:
                        await ctx.bot.send_message(chat_id=chat_id, text=msg)
                    except Exception as _exc:
                        logger.debug("%s: %s", type(_exc).__name__, _exc)
                        pass
            last_seen_trigger_id = max(last_seen_trigger_id, tid)
    except Exception as e:
        logger.warning(f"trigger check error: {e}")

async def background_daily_digest(ctx: ContextTypes.DEFAULT_TYPE):
    """Daily minimalist digest: fleet totals+deltas, plus top outliers —
    NOT a full per-account listing. Replaces the old hourly totals-only
    summary (background_hourly_summary), which sent every hour with a
    24h-trailing delta — mostly repeating the same numbers, and gave no
    way to see WHICH accounts were driving the change.

    Design agreed with Alexander 2026-08-11 (AGENTS.md, 'Дизайн
    аналитики/уведомлений'): deltas over absolute totals, outliers over
    exhaustive per-account listing, daily cadence instead of hourly.

    Scoped honestly for what the existing /metrics/overview data actually
    supports: outlier ranking uses follower delta as the primary signal
    (simple, explainable — avoids inventing a weighted cross-metric score).
    "New accounts today" and "no activity for N days" from the original
    mockup aren't computable from a single 24h overview snapshot (the
    former needs a backend flag, the latter needs per-account history
    queries) — this reports "no change in today's 24h window" instead,
    which is honest about what one snapshot comparison can actually show.
    """
    try:
        async with aiohttp.ClientSession() as session:
            data = await api_get(session, "/api/ig-web-upload/metrics/overview?hours=24")
            data_1h = await api_get(session, "/api/ig-web-upload/metrics/overview?hours=1")
        if not data.get("ok"):
            return
        t = data.get("total", {})
        dl = data.get("delta_24h", {})
        # The API keys this "delta_24h" regardless of the hours= param
        # actually passed — with hours=1 it's really the last-hour delta.
        dl_1h = data_1h.get("delta_24h", {}) if data_1h.get("ok") else {}
        accounts = data.get("accounts", [])
        if not accounts:
            return

        lines = [
            "📊 За сегодня",
            f"{len(accounts)} аккаунтов",
            f"Просмотры: {fmt_delta(dl.get('views', 0))} · "
            f"Подписчики: {fmt_delta(dl.get('followers', 0))} · "
            f"Лайки: {fmt_delta(dl.get('likes', 0))}",
        ]
        if data_1h.get("ok"):
            lines.append(
                f"За последний час: "
                f"{fmt_delta(dl_1h.get('views', 0))} просм · "
                f"{fmt_delta(dl_1h.get('followers', 0))} подп · "
                f"{fmt_delta(dl_1h.get('likes', 0))} лайк"
            )

        def _fol_delta(acc):
            return int((acc.get("delta") or {}).get("followers", 0) or 0)

        def _views_delta(acc):
            return int((acc.get("delta") or {}).get("views", 0) or 0)

        def _stalled(acc):
            d = acc.get("delta") or {}
            return (
                int(d.get("followers", 0) or 0) == 0
                and int(d.get("views", 0) or 0) == 0
                and int(d.get("likes", 0) or 0) == 0
            )

        # Attention first (declining or flat), then growers from whatever
        # remains — an account can't be both "growing" and "needs
        # attention" in the same digest, even if one metric ticked up
        # while followers declined overall.
        decliners = sorted(
            (a for a in accounts if _fol_delta(a) < 0), key=_fol_delta
        )[:5]
        stalled = [a for a in accounts if _stalled(a)]
        attention = decliners + [a for a in stalled if a not in decliners]
        attention = attention[:5]
        attention_names = {a["name"] for a in attention}

        growers = sorted(
            (
                a for a in accounts
                if a["name"] not in attention_names
                and (_fol_delta(a) > 0 or _views_delta(a) > 0)
            ),
            key=_fol_delta,
            reverse=True,
        )[:5]

        if growers:
            parts = []
            for a in growers:
                fol, views = _fol_delta(a), _views_delta(a)
                if fol > 0:
                    parts.append(f"@{a['name']} +{fmt(fol)} подп")
                elif views > 0:
                    parts.append(f"@{a['name']} +{fmt(views)} просмотров")
            if parts:
                lines.append("")
                lines.append("🔥 " + " · ".join(parts))

        if attention:
            parts = []
            for a in attention:
                fol = _fol_delta(a)
                if fol < 0:
                    parts.append(f"@{a['name']} {fol} подп")
                elif _stalled(a):
                    parts.append(f"@{a['name']} без изменений сегодня")
            if parts:
                lines.append("⚠️ " + " · ".join(parts))

        msg = "\n".join(lines)
        for chat_id in authorized_chat_ids():
            try:
                await ctx.bot.send_message(chat_id=chat_id, text=msg)
            except Exception as _exc:
                logger.debug("%s: %s", type(_exc).__name__, _exc)
                pass
    except Exception as e:
        logger.warning(f"daily digest error: {e}")


# ─── Background: Upload/Story Completion Notifications ────────────────────────

# Track which run_ids we've already reported
_REPORTED_RUNS_FILE = DATA_DIR / "telegram_reported_runs.json"


def _load_reported_runs() -> set[str]:
    """Run IDs already announced, persisted across bot restarts.

    Diagnosed 2026-08-14: this set lived only in memory, so a bot restart
    wiped it and every finished run got announced a second time —
    Alexander saw "Рилсы: @akpinarniy.azi.475 (3 залито)" twice in the
    same minute. The bot restarts often (crash-loop guard, manual
    restarts, Conflict respawns), so in-memory alone was never going to
    hold.
    """
    try:
        if _REPORTED_RUNS_FILE.exists():
            data = json.loads(_REPORTED_RUNS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return {str(x) for x in data}
    except Exception as e:
        logger.debug("could not load reported runs: %s", e)
    return set()


def _save_reported_runs(runs: set[str]) -> None:
    try:
        # Keep the most recent 200 — the file is a dedupe guard, not history.
        trimmed = list(runs)[-200:]
        tmp = _REPORTED_RUNS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(trimmed, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, _REPORTED_RUNS_FILE)
    except Exception as e:
        logger.debug("could not save reported runs: %s", e)


_reported_runs: set[str] = _load_reported_runs()
# Buffer for batching notifications — collects events, sends one combined message
_pending_notifications: list[str] = []
_last_notification_flush: float = 0

async def background_check_completion(ctx: ContextTypes.DEFAULT_TYPE):
    """Check every 30s for finished jobs. Batch notifications into one message."""
    global _pending_notifications, _last_notification_flush
    import time as _time
    try:
        async with aiohttp.ClientSession() as session:
            data = await api_get(session, "/api/ig-web-upload/overview")
        if not data.get("ok"):
            return

        jobs = data.get("jobs", [])
        new_events: list[str] = []
        stopped_accounts: list[str] = []

        for job in jobs[:20]:
            status = str(job.get("status") or "")
            run_id = str(job.get("run_id") or "")
            if not run_id or run_id in _reported_runs:
                continue
            if status not in ("success", "failed", "partial_success", "manual_required",
                              "stopped", "cooldown", "uploaded_unverified",
                              "submitted_unverified", "empty_selection"):
                continue

            _reported_runs.add(run_id)
            if len(_reported_runs) > 200:
                _reported_runs.clear()
                _reported_runs.add(run_id)
            _save_reported_runs(_reported_runs)

            account = str(job.get("account_name") or "?")
            posted = int(job.get("posted_count") or 0)
            error = str(job.get("last_error") or "")
            current_step = str(job.get("current_step") or "")
            is_story = "story" in current_step.lower() or "story" in str(job.get("label") or "").lower()

            # Don't report "posted: 0" as success — it means nothing was uploaded
            if status == "success" and posted == 0 and not is_story:
                # This is a warmup-only or no-content run, skip notification
                continue

            if status == "success":
                if is_story:
                    line = f"✅ История: @{account}"
                else:
                    line = f"✅ Рилсы: @{account} ({posted} залито)"
            elif status == "partial_success":
                if is_story:
                    line = f"⚠️ История (частично): @{account}"
                else:
                    line = f"⚠️ Рилсы (частично): @{account} — {posted}/{error[:60]}"
            elif status == "failed":
                line = f"❌ @{account}: {error[:80]}" if error else f"❌ @{account}"
            elif status == "manual_required":
                # Diagnosed 2026-08-14: sending this with an empty error
                # produced bare "🟡 @account: " messages with nothing after
                # the colon. It happens when the poll catches a job mid
                # transition — status already manual_required, last_error
                # not filled in (or already cleared) — and moments later
                # the job settles as success. Announcing a problem with no
                # description, for a job that turns out fine, is worse than
                # staying quiet: skip it and let the next poll report the
                # settled state.
                if not error:
                    _reported_runs.discard(run_id)
                    continue
                line = f"🟡 @{account}: {error[:80]}"
            elif status == "stopped":
                # Diagnosed 2026-08-14: these are the "red circles" that
                # flooded Alexander's chat — 🛑 renders as a red dot in
                # Telegram, and one arrived per account every time a run
                # was stopped. Stopping is his own deliberate action, so
                # per-account confirmations tell him nothing he doesn't
                # already know. Collected and reported as a single line
                # below instead.
                stopped_accounts.append(account)
                continue
            elif status == "empty_selection":
                continue  # Not useful for user
            else:
                line = f"❓ {status}: @{account}"

            new_events.append(line)

        # One line for everything stopped in this batch, instead of one
        # message per account.
        if stopped_accounts:
            if len(stopped_accounts) <= 3:
                new_events.append("🛑 Остановлено: " + ", ".join(f"@{a}" for a in stopped_accounts))
            else:
                new_events.append(f"🛑 Остановлено аккаунтов: {len(stopped_accounts)}")

        # Add to pending buffer
        _pending_notifications.extend(new_events)

        # Flush: send batched message if 2 min passed OR 5+ events queued
        now = _time.time()
        should_flush = (
            _pending_notifications and (
                now - _last_notification_flush >= 120 or
                len(_pending_notifications) >= 5
            )
        )

        if should_flush:
            msg = "\n".join(_pending_notifications[:20])
            if len(_pending_notifications) > 20:
                msg += f"\n... и ещё {len(_pending_notifications) - 20}"
            _pending_notifications.clear()
            _last_notification_flush = now

            for chat_id in authorized_chat_ids():
                try:
                    await ctx.bot.send_message(chat_id=chat_id, text=msg)
                except Exception as _exc:
                    logger.debug("%s: %s", type(_exc).__name__, _exc)
                    pass

    except Exception as e:
        logger.warning(f"completion check error: {e}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("metrics", cmd_metrics))
    app.add_handler(CommandHandler("upload", cmd_upload))
    app.add_handler(CommandHandler("stories", cmd_stories))
    app.add_handler(CommandHandler("login", cmd_login))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("session", cmd_session))
    app.add_handler(CommandHandler("delete_banned", cmd_delete_banned))
    app.add_handler(CommandHandler("stop", cmd_stop))
    # Admin commands
    app.add_handler(CommandHandler("add_user", cmd_add_user))
    app.add_handler(CommandHandler("remove_user", cmd_remove_user))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CallbackQueryHandler(callback_handler))

    # Background jobs — require python-telegram-bot[job-queue]
    job_queue = app.job_queue
    if job_queue:
        job_queue.run_repeating(background_check_triggers, interval=600, first=30)
        job_queue.run_daily(
            background_daily_digest,
            time=dt_time(hour=9, minute=0, tzinfo=ZoneInfo("Europe/Moscow")),
        )
        job_queue.run_repeating(background_check_completion, interval=30, first=15)
        logger.info(f"Background jobs: triggers 10min, hourly summary, completion check 30s. Users: {len(AUTHORIZED_USERS)}")
    else:
        logger.warning("No JobQueue! Install: pip install \"python-telegram-bot[job-queue]\"")

    logger.info(f"Бот запущен. Авторизованных пользователей: {len(AUTHORIZED_USERS)}")

    # Set native Telegram command menu via post_init (async, correct way)
    async def _post_init(app: Application) -> None:
        from telegram import BotCommand
        try:
            await app.bot.set_my_commands([
                BotCommand("status", "📋 Аккаунты"),
                BotCommand("metrics", "📊 Метрики"),
                BotCommand("upload", "🚀 Залить рилсы"),
                BotCommand("stories", "📸 Истории"),
                BotCommand("login", "🔐 Логин"),
                BotCommand("session", "🔍 Проверить сессии"),
                BotCommand("check", "📈 Собрать метрики"),
                BotCommand("delete_banned", "🗑 Удалить забаненные"),
                BotCommand("stop", "🛑 Стоп всё"),
                BotCommand("users", "👥 Пользователи бота"),
            ])
            logger.info("Native Telegram menu set")
        except Exception as e:
            logger.warning(f"Failed to set native menu: {e}")

    app.post_init = _post_init
    app.run_polling(allowed_updates=Update.ALL_TYPES)


def run_forever():
    """Run bot with auto-restart on crash. Gives up after repeated fast
    crashes instead of looping forever silently (same failure pattern the
    PowerShell launchers had before start_service_template.ps1 got a
    crash-loop guard — this is the Python-side equivalent)."""
    import time

    crash_count = 0
    while True:
        start_time = time.monotonic()
        try:
            logger.info("Запуск бота...")
            main()
        except KeyboardInterrupt:
            logger.info("Остановка по запросу")
            break
        except Exception as e:
            elapsed = time.monotonic() - start_time
            if elapsed >= 30:
                crash_count = 0
            else:
                crash_count += 1
            if crash_count >= 5:
                logger.error(
                    f"Бот упал: {e}. Crash-loop: 5 быстрых падений подряд, "
                    f"автоперезапуск остановлен. Разберись руками, потом "
                    f"запусти заново."
                )
                break
            logger.error(f"Бот упал: {e}. Перезапуск через 10 сек...")
            time.sleep(10)


if __name__ == "__main__":
    run_forever()
