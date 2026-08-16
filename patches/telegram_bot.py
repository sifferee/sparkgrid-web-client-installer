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


def _group_accounts_by_reason(
    accounts: list, reason_key: str, *, max_groups: int = 5, max_names: int = 3
) -> list[str]:
    """Turn a flat list of accounts into compact grouped lines like
    "3 — Instagram отклонил учётные данные (@a, @b, @c)" instead of one
    bare count. Diagnosed 2026-08-15: the "6 — ошибка" bucket in the mass-
    upload summary was a naked number with zero detail — Alexander had no
    way to tell from the bot alone whether that meant 6 accounts with a
    stale login failure, 6 with no free proxy, or 6 different problems
    each affecting one account. Grouping by the actual reason text (most
    common first) answers that without dumping 6 raw error strings.

    Groups beyond max_groups collapse into one "other reasons" line so a
    long tail of one-off errors can't blow up the message."""
    groups: dict[str, list[str]] = {}
    for a in accounts:
        name = str(a.get("name") or "?")
        reason = str(a.get(reason_key) or "").strip() or "причина не указана"
        # Diagnosed 2026-08-16: some error messages embed the account's
        # own name as a prefix — e.g. instagram_web_upload.py builds
        # f"{name}: no ready {content_mode} content". Grouped on the raw
        # text, every account's version of the exact same underlying
        # problem ("out of content") counts as its own unique reason —
        # one line each, seen live as three separate "X: no ready scale
        # content" lines instead of one "3 — no ready scale content
        # (@a, @b, @c)". Stripping a leading "{own name}: " before using
        # the text as a group key restores the intended grouping without
        # touching what's actually stored in last_error.
        prefix = f"{name}: "
        if reason.startswith(prefix):
            reason = reason[len(prefix):]
        groups.setdefault(reason[:80], []).append(name)
    ordered = sorted(groups.items(), key=lambda kv: len(kv[1]), reverse=True)
    lines = []
    for reason, names in ordered[:max_groups]:
        names_part = ", ".join(f"@{n}" for n in names[:max_names])
        if len(names) > max_names:
            names_part += f" +{len(names) - max_names}"
        lines.append(f"{len(names)} — {reason} ({names_part})")
    if len(ordered) > max_groups:
        rest_count = sum(len(names) for _, names in ordered[max_groups:])
        rest_kinds = len(ordered) - max_groups
        lines.append(f"{rest_count} — другие причины ({rest_kinds} видов)")
    return lines

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
    # See background_daily_digest() for the diagnosis: "Просмотры" above
    # already excludes top-12 rotation drops from the growth number, this
    # just explains why the raw per-account sum wouldn't match it.
    rotated_out = int(data.get("views_rotated_out", 0) or 0)
    if rotated_out > 0:
        msg += f"   (+{fmt(rotated_out)} просм. выпало из топ-12, не потеря)\n"
    # Change since the previous check, alongside the 24h totals. When
    # checks run about hourly, the 24h numbers barely move between two of
    # them and read as if nothing is happening — this shows what the
    # latest check actually found. Hidden when there's nothing to report
    # (first ever check, or genuinely no movement).
    sl = data.get("delta_since_last") or {}
    s_fol = int(sl.get("followers") or 0)
    s_views = int(sl.get("views") or 0)
    s_likes = int(sl.get("likes") or 0)
    if s_fol or s_views or s_likes:
        msg += (f"\nС прошлой проверки: {fmt_delta(s_views)} просм · "
                f"{fmt_delta(s_fol)} подп · {fmt_delta(s_likes)} лайк\n")

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
    # Diagnosed 2026-08-16: this used to list EVERY logged_in, non-banned
    # account as an individual button — including accounts sitting in
    # cooldown, which is most of the fleet most of the time. With a large
    # account count that's a wall of buttons to scroll through for nothing:
    # tapping a cooldown account individually still hits the exact same
    # backend gate (ig_signals.cooldown_left in instagram_web_upload.py) it
    # would in a batch run, so listing it here buys nothing. Only the
    # "error" bucket (stale web_upload_last_error) genuinely benefits from
    # an individual tap — that's the documented way to give a stuck account
    # another shot, bypassing the mass-upload categorization that otherwise
    # excludes it every time (see _categorize_for_mass_upload). "Ready"
    # accounts are already one tap away via "Все ready" and don't need
    # their own row either.
    buckets = _categorize_for_mass_upload(accounts)
    keyboard = []
    keyboard.append([InlineKeyboardButton("🚀 Все ready", callback_data="upload_all")])
    for a in buckets["error"]:
        keyboard.append([InlineKeyboardButton(f"@{a['name']}", callback_data=f"upload:{a['name']}")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="cmd:start")])
    hidden_cooldown = len(buckets["cooldown"])
    note = f"Выбери аккаунты для залива. Ready: {len(buckets['ready'])} (кнопка выше). С ошибкой ниже: {len(buckets['error'])}."
    if hidden_cooldown:
        note += f"\nВ кулдауне сейчас: {hidden_cooldown} — не показаны, тап всё равно отклонит сервер."
    await _reply(update_or_query, note, reply_markup=InlineKeyboardMarkup(keyboard))

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
                # Was: f"{len(buckets['error'])} — ошибка" — a bare count
                # that told Alexander nothing about WHY those accounts
                # were excluded. These are accounts that ARE logged_in
                # and NOT in cooldown, but got parked here because
                # web_upload_last_error is still non-empty from whatever
                # last touched them (a failed login, a failed upload
                # cycle, a proxy problem, ...) — and nothing auto-clears
                # that field, so once an account lands here it stays
                # excluded from every future mass-upload run until it
                # either succeeds via a manual single-account retry (the
                # per-account @name button skips this categorization
                # entirely) or something explicitly clears the error.
                skip_lines.extend(
                    _group_accounts_by_reason(buckets["error"], "web_upload_last_error")
                )

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
                # Use what the API actually accepted (result["accounts"]),
                # not ready_names we asked for — without_suspended_accounts()
                # in app.py's /start can still drop a few between "we sent
                # this list" and "this is what actually launched", and the
                # batch summary's denominator needs to match reality or it
                # will sit reporting "N missing" forever for accounts that
                # were never actually started.
                _register_pending_batch(run_id, result.get("accounts") or ready_names)
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
        # Diagnosed 2026-08-15: total_views only sums each account's latest
        # 12 posts. A new reel bumps an old one out of that window and its
        # views vanish from the number — not a real loss. get_overview()
        # now excludes those drops from delta_24h.views (growth-only) and
        # reports their magnitude here so a negative-looking day doesn't
        # get reported as one when it wasn't.
        rotated_out = int(data.get("views_rotated_out", 0) or 0)
        if rotated_out > 0:
            lines.append(
                f"ℹ️ +{fmt(rotated_out)} просм. выпало из выборки "
                f"(старый рилс вне топ-12 после нового поста, не потеря)"
            )
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

# Track which job IDs we've already announced individually.
#
# Diagnosed 2026-08-15: this used to be keyed by job["run_id"] and called
# "_reported_runs" — but run_id is NOT unique per job. connection_scheduler.py
# assigns ONE run_id per batch trigger (SPARKGRID_RUN_ID is set once in
# app.py's start_process(), then inherited unchanged via os.environ.copy()
# into every per-account subprocess it launches — traced end to end through
# app.py -> connection_scheduler.py -> instagram_web_upload.py). So a
# "Залить рилсы" run on 16 accounts produces 16 job rows that all share the
# same run_id. The old code did `if run_id in _reported_runs: continue` —
# meaning the FIRST account to finish claimed that run_id, and every other
# account sharing the same batch was silently skipped forever after,
# regardless of its own outcome. That's why only "1-2 accounts" ever showed
# up out of a much larger run. Each job row's own "id" column IS unique per
# job — that's the correct dedupe key.
_REPORTED_JOBS_FILE = DATA_DIR / "telegram_reported_jobs.json"
_REPORTED_JOBS_MAX = 200


def _load_reported_jobs() -> dict[str, bool]:
    """Job IDs already announced, persisted across bot restarts, in the
    order they were first seen.

    Diagnosed 2026-08-14: this set lived only in memory, so a bot restart
    wiped it and every finished run got announced a second time —
    Alexander saw "Рилсы: @akpinarniy.azi.475 (3 залито)" twice in the
    same minute. The bot restarts often (crash-loop guard, manual
    restarts, Conflict respawns), so in-memory alone was never going to
    hold.

    Diagnosed 2026-08-16: this used to be a plain set(), which has no
    defined insertion order in Python. Both the trim logic below AND
    _save_reported_jobs's list(jobs)[-200:] were silently relying on an
    ordering a set never guaranteed — the trim could evict a job reported
    a second ago while keeping one from an hour ago. A dict (insertion
    order is guaranteed since Python 3.7) makes "oldest" and "newest"
    actually mean what the code assumes they mean.
    """
    try:
        if _REPORTED_JOBS_FILE.exists():
            data = json.loads(_REPORTED_JOBS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return {str(x): True for x in data}
    except Exception as e:
        logger.debug("could not load reported jobs: %s", e)
    return {}


def _save_reported_jobs(jobs: dict[str, bool]) -> None:
    try:
        # Keep the most recent 200, in true insertion order — the file is
        # a dedupe guard, not history.
        trimmed = list(jobs.keys())[-_REPORTED_JOBS_MAX:]
        tmp = _REPORTED_JOBS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(trimmed, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, _REPORTED_JOBS_FILE)
    except Exception as e:
        logger.debug("could not save reported jobs: %s", e)


_reported_jobs: dict[str, bool] = _load_reported_jobs()
# Buffer for batching notifications — collects events, sends one combined message
_pending_notifications: list[str] = []
_last_notification_flush: float = 0

# ─── Background: Batch (mass-upload) Completion Summary ───────────────────────
#
# Alexander's ask 2026-08-15: after "Залить рилсы" on e.g. 30 accounts, he
# wants one final tally — "30/30 posted" or "25/30 posted, 5 accounts —
# reason X" — instead of only ever seeing the first account's individual
# line (see the dedupe bug above) or having to guess from a trickle of
# per-account notifications spread over the run's whole duration.
#
# This piggybacks on the same run_id that caused the dedupe bug — since
# every account in one "Залить рилсы" trigger really does share it, that's
# exactly the grouping key a batch summary needs. _register_pending_batch()
# is called right after a mass trigger starts (with the account list the
# API actually accepted); background_check_completion() then records each
# job's outcome under its run_id as jobs finish, and fires one summary once
# every expected account has a result (or after PENDING_BATCH_MAX_AGE_MIN
# as a safety net, in case some account's worker crashed before ever
# writing a job row at all).
_PENDING_BATCHES_FILE = DATA_DIR / "telegram_pending_batches.json"
PENDING_BATCH_MAX_AGE_MIN = 180  # generous: large batches with limited
# browser_parallel can legitimately take over an hour to work through.


def _load_pending_batches() -> dict:
    try:
        if _PENDING_BATCHES_FILE.exists():
            data = json.loads(_PENDING_BATCHES_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception as e:
        logger.debug("could not load pending batches: %s", e)
    return {}


def _save_pending_batches(batches: dict) -> None:
    try:
        tmp = _PENDING_BATCHES_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(batches, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, _PENDING_BATCHES_FILE)
    except Exception as e:
        logger.debug("could not save pending batches: %s", e)


_pending_batches: dict = _load_pending_batches()


def _register_pending_batch(run_id: str, expected_names: list) -> None:
    """Call right after a mass trigger (e.g. upload_all) starts. expected_names
    should be exactly the account list the API accepted (result["accounts"]
    or the same ready_names sent), so the batch summary's denominator
    matches what was actually launched, not what was merely selected."""
    run_id = str(run_id or "").strip()
    names = [str(n) for n in (expected_names or []) if n]
    if not run_id or not names:
        return
    _pending_batches[run_id] = {
        "expected": names,
        "seen": {},
        "triggered_at": datetime.now().isoformat(timespec="seconds"),
    }
    _save_pending_batches(_pending_batches)


def _format_batch_summary(expected: list, seen: dict) -> str:
    """expected: account names launched. seen: {account_name: {"status","posted","error"}}
    for every account that reached a terminal state. Accounts missing from
    seen (never finished, or the safety-net timeout hit) get their own line."""
    total = len(expected)
    ok_names = {
        name for name, v in seen.items()
        if v.get("status") == "success" and int(v.get("posted") or 0) > 0
    }
    posted_total = sum(int(v.get("posted") or 0) for v in seen.values())
    lines = [f"📦 Итог заливки: {len(ok_names)}/{total} аккаунтов, видео залито: {posted_total}"]

    problem_accounts = []
    for name, v in seen.items():
        if name in ok_names:
            continue
        status = str(v.get("status") or "")
        error = str(v.get("error") or "")
        if status == "partial_success":
            reason = f"частично ({v.get('posted', 0)} залито) — {error[:60]}" if error else f"частично ({v.get('posted', 0)} залито)"
        elif status == "cooldown":
            reason = "кулдаун сработал уже после запуска (гонка между ботом и сервером)"
        elif error:
            reason = error
        else:
            reason = status or "неизвестно"
        problem_accounts.append({"name": name, "_reason": reason})
    if problem_accounts:
        lines.extend(_group_accounts_by_reason(problem_accounts, "_reason"))

    missing = [n for n in expected if n not in seen]
    if missing:
        shown = ", ".join(f"@{n}" for n in missing[:3])
        if len(missing) > 3:
            shown += f" +{len(missing) - 3}"
        lines.append(f"{len(missing)} — ещё не завершились или воркер не отчитался ({shown})")
    return "\n".join(lines)



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

        # Reportable terminal statuses — a job sitting in "running"/"queued"/
        # "starting" isn't finished yet and shouldn't be touched here.
        TERMINAL_STATUSES = (
            "success", "failed", "partial_success", "manual_required",
            "stopped", "cooldown", "uploaded_unverified",
            "submitted_unverified", "empty_selection",
        )

        # Diagnosed 2026-08-16: overview()'s SQL already fetches the 100
        # most recent job rows (LIMIT 100 in app.py), but this used to
        # only look at the top 20 of those. Fine almost always — new jobs
        # keep getting created, so anything unreported eventually rises
        # into that window on a later poll. It broke tonight: a single
        # mass status change (62 accounts hit "stopped" within the same
        # second from one batch operation) with no further job creation
        # afterward meant the bottom 42 of those 62 never had anything to
        # push them into the top 20 — Alexander's Telegram showed
        # "Остановлено аккаунтов: 20" while 62 had actually stopped.
        # Matching the SQL's own LIMIT removes the mismatch instead of
        # guessing at a bigger arbitrary number.
        for job in jobs[:100]:
            status = str(job.get("status") or "")
            is_terminal = status in TERMINAL_STATUSES
            job_key = str(job.get("id") or "")
            job_run_id = str(job.get("run_id") or "")
            account = str(job.get("account_name") or "?")
            posted = int(job.get("posted_count") or 0)
            error = str(job.get("last_error") or "")

            # Feed the batch tracker independently of the individual-
            # notification dedupe below, so a pending batch summary isn't
            # blocked just because this account's own line already went
            # out on an earlier poll. Overwrite-on-each-terminal-sighting
            # (not skip-if-already-seen) so a later, more final outcome
            # (e.g. a retried job that eventually succeeds) replaces an
            # earlier one instead of being stuck behind it.
            if is_terminal and job_run_id in _pending_batches:
                batch = _pending_batches[job_run_id]
                if account in batch.get("expected", []):
                    batch.setdefault("seen", {})[account] = {
                        "status": status, "posted": posted, "error": error,
                    }
                    _save_pending_batches(_pending_batches)

            if not job_key or job_key in _reported_jobs or not is_terminal:
                continue

            _reported_jobs[job_key] = True
            if len(_reported_jobs) > _REPORTED_JOBS_MAX:
                # Evict only the actually-oldest entries (dict preserves
                # insertion order) — never the ones just added in this
                # very poll, unlike the old set().clear() which wiped
                # everything, including whatever this same loop had just
                # added, the moment the count crossed 200.
                for old_key in list(_reported_jobs.keys())[:-_REPORTED_JOBS_MAX]:
                    del _reported_jobs[old_key]
            _save_reported_jobs(_reported_jobs)

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
                    _reported_jobs.pop(job_key, None)
                    continue
                line = f"🟡 @{account}: {error[:80]}"
            elif status == "cooldown":
                # Diagnosed 2026-08-15: this is the backend's OWN cooldown
                # check (ig_signals.cooldown_left in instagram_web_upload.py),
                # separate from the bot's own pre-flight filter
                # (_cooldown_hours_remaining) used to build ready_names.
                # The two are computed differently and can disagree — this
                # fires when the bot thought an account was ready enough to
                # launch, but the backend's own gate skipped it anyway.
                line = f"⏳ @{account}: кулдаун сработал уже после запуска" + (f" — {error[:60]}" if error else "")
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

        # ── Batch (mass-upload) completion summaries ──
        # A batch is done once every account launched for it has reached a
        # terminal state, or after PENDING_BATCH_MAX_AGE_MIN as a safety net
        # (covers an account whose worker crashed before ever writing a job
        # row at all — vanishingly rare, since create_job() happens right
        # at the start of account_lane(), but "wait forever" is worse than
        # "report late" if it ever does happen).
        batches_changed = False
        now_dt = datetime.now()
        for run_id in list(_pending_batches.keys()):
            batch = _pending_batches[run_id]
            expected = batch.get("expected", [])
            seen = batch.get("seen", {})
            try:
                age_min = (now_dt - datetime.fromisoformat(str(batch.get("triggered_at") or ""))).total_seconds() / 60.0
            except Exception:
                age_min = 0.0
            if len(seen) >= len(expected) or age_min > PENDING_BATCH_MAX_AGE_MIN:
                new_events.append(_format_batch_summary(expected, seen))
                del _pending_batches[run_id]
                batches_changed = True
        if batches_changed:
            _save_pending_batches(_pending_batches)


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
