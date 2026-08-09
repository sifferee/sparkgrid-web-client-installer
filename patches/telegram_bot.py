"""
SparkGrid Telegram Bot.

Client to SparkGrid API — manages uploads, stories, login, metrics.
Does NOT touch server code, browsers, or DB directly. Only HTTP API calls.

Commands:
  /start     — welcome
  /status    — account overview
  /metrics    — metrics dashboard summary
  /upload    — start reel upload (inline keyboard)
  /stories   — post stories (inline keyboard)
  /login     — auto login (inline keyboard)
  /check     — run metrics checker now
  /stop      — stop all processes

Passive: checks for new story triggers every 10 min, sends hourly summary.
"""

import asyncio
import logging
import os
import sys
from datetime import datetime

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
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
API_URL = os.environ.get("SPARKGRID_API_URL", "http://127.0.0.1:8770")

if not BOT_TOKEN:
    print("ERROR: set TELEGRAM_BOT_TOKEN env var")
    sys.exit(1)

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
    logging.FileHandler(os.path.join(log_dir, "telegram_bot.log"), encoding="utf-8").setLevel(logging.INFO)
    logger.addHandler(logging.FileHandler(os.path.join(log_dir, "telegram_bot.log"), encoding="utf-8"))
except Exception:
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

# ─── API Client ───────────────────────────────────────────────────────────────

async def api_get(session, path):
    try:
        async with session.get(f"{API_URL}{path}", timeout=aiohttp.ClientTimeout(total=30)) as resp:
            return await resp.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

async def api_post(session, path, body=None):
    try:
        async with session.post(
            f"{API_URL}{path}",
            json=body or {},
            timeout=aiohttp.ClientTimeout(total=120),
        ) as resp:
            return await resp.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ─── Commands ──────────────────────────────────────────────────────────────────

WELCOME = """🤖 *SparkGrid Бот*

Команды:
/status — список аккаунтов
/metrics — метрика (подписчики, просмотры, лайки)
/upload — запуск залива рилсов
/stories — залив историй
/login — авто-логин аккаунтов
/check — запустить проверку метрик
/session — проверить сессии (активные vs протухшие)
/stop — остановить все процессы"""

async def cmd_start(update: Update, ctx):
    await update.message.reply_text(WELCOME, parse_mode="Markdown")

async def cmd_status(update: Update, ctx):
    async with aiohttp.ClientSession() as session:
        data = await api_get(session, "/api/ig-web-upload/overview")
    if not data.get("ok"):
        await update.message.reply_text("❌ SparkGrid недоступен")
        return
    accounts = data.get("accounts", [])
    if not accounts:
        await update.message.reply_text("Нет аккаунтов")
        return
    lines = ["*Аккаунты:*"]
    for a in accounts:
        login = a.get("web_upload_login_status", "?")
        priv = a.get("web_privacy_status", "?")
        emoji = "✅" if login == "logged_in" else "❌" if login in ("suspended", "failed") else "⚠️"
        lines.append(f"{emoji} @{a['name']} | {login} | {priv}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_metrics(update: Update, ctx):
    async with aiohttp.ClientSession() as session:
        data = await api_get(session, "/api/ig-web-upload/metrics/overview?hours=24")
    if not data.get("ok"):
        await update.message.reply_text("❌ Нет данных метрики. Запусти /check")
        return
    t = data.get("total", {})
    dl = data.get("delta_24h", {})
    msg = (
        f"📊 *Итого*\n"
        f"Подписчики: {fmt(t.get('followers',0))} ({fmt_delta(dl.get('followers',0))})\n"
        f"Просмотры: {fmt(t.get('views',0))} ({fmt_delta(dl.get('views',0))})\n"
        f"Лайки: {fmt(t.get('likes',0))} ({fmt_delta(dl.get('likes',0))})\n"
        f"Комментарии: {fmt(t.get('comments',0))} ({fmt_delta(dl.get('comments',0))})\n"
    )
    accounts = data.get("accounts", [])
    if accounts:
        msg += "\n*По аккаунтам:*"
        for a in accounts[:15]:
            name = a.get("name", "?")
            fol = a.get("followers", 0)
            views = a.get("total_views", 0)
            likes = a.get("total_likes", 0)
            msg += f"\n@{name}: {fmt(fol)} подп | {fmt(views)} просм | {fmt(likes)} лайков"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_upload(update: Update, ctx):
    """Show inline keyboard for selecting upload target."""
    async with aiohttp.ClientSession() as session:
        data = await api_get(session, "/api/ig-web-upload/overview")
    if not data.get("ok"):
        await update.message.reply_text("❌ SparkGrid недоступен")
        return
    accounts = [a for a in data.get("accounts", []) if a.get("web_upload_login_status") == "logged_in"]
    if not accounts:
        await update.message.reply_text("Нет готовых аккаунтов (logged_in)")
        return
    keyboard = []
    keyboard.append([InlineKeyboardButton("🚀 Все ready", callback_data="upload_all")])
    for a in accounts:
        keyboard.append([InlineKeyboardButton(f"@{a['name']}", callback_data=f"upload:{a['name']}")])
    keyboard.append([InlineKeyboardButton("Отмена", callback_data="cancel")])
    await update.message.reply_text(
        "Выбери аккаунты для залива:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

async def cmd_stories(update: Update, ctx):
    """Show inline keyboard for story posting."""
    async with aiohttp.ClientSession() as session:
        data = await api_get(session, "/api/ig-web-upload/overview")
    if not data.get("ok"):
        await update.message.reply_text("❌ SparkGrid недоступен")
        return
    accounts = [a for a in data.get("accounts", []) if a.get("web_upload_login_status") == "logged_in"]
    if not accounts:
        await update.message.reply_text("Нет готовых аккаунтов")
        return
    keyboard = []
    keyboard.append([InlineKeyboardButton("📸 Все", callback_data="stories_all")])
    for a in accounts:
        keyboard.append([InlineKeyboardButton(f"@{a['name']}", callback_data=f"stories:{a['name']}")])
    keyboard.append([InlineKeyboardButton("Отмена", callback_data="cancel")])
    await update.message.reply_text(
        "Выбери аккаунты для историй:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

async def cmd_login(update: Update, ctx):
    """Show inline keyboard for auto login."""
    async with aiohttp.ClientSession() as session:
        data = await api_get(session, "/api/ig-web-upload/overview")
    if not data.get("ok"):
        await update.message.reply_text("❌ SparkGrid недоступен")
        return
    accounts = data.get("accounts", [])
    if not accounts:
        await update.message.reply_text("Нет аккаунтов")
        return
    keyboard = []
    keyboard.append([InlineKeyboardButton("🔐 Все", callback_data="login_all")])
    for a in accounts:
        keyboard.append([InlineKeyboardButton(f"@{a['name']}", callback_data=f"login:{a['name']}")])
    keyboard.append([InlineKeyboardButton("Отмена", callback_data="cancel")])
    await update.message.reply_text(
        "Выбери аккаунты для авто-логина:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

async def cmd_check(update: Update, ctx):
    async with aiohttp.ClientSession() as session:
        data = await api_post(session, "/api/ig-web-upload/metrics/run")
    if data.get("ok"):
        await update.message.reply_text("✅ Проверка метрик запущена. Результаты через ~2 мин.")
    else:
        await update.message.reply_text(f"❌ {data.get('error', 'ошибка')}")

async def cmd_session(update: Update, ctx):
    """Check session validity — which accounts are still logged in vs expired."""
    async with aiohttp.ClientSession() as session:
        data = await api_get(session, "/api/ig-web-upload/overview")
    if not data.get("ok"):
        await update.message.reply_text("❌ SparkGrid недоступен")
        return
    accounts = data.get("accounts", [])
    if not accounts:
        await update.message.reply_text("Нет аккаунтов")
        return
    # Group by status
    active = []
    expired = []
    other = []
    for a in accounts:
        status = a.get("web_upload_login_status", "unknown")
        name = a.get("name", "?")
        last_login = a.get("web_upload_last_login_at", "")
        if status == "logged_in":
            active.append(f"✅ @{name} (last: {last_login[:16] if last_login else '—'})")
        elif status in ("incorrect_credentials", "consent_failed", "manual_required", "suspended", "browser_internal_error"):
            expired.append(f"❌ @{name} — {status}")
        else:
            other.append(f"⚠️ @{name} — {status}")
    msg = "*Проверка сессий*\n"
    if active:
        msg += f"\n🟢 Активные ({len(active)}):\n" + "\n".join(active[:10]) + "\n"
    if expired:
        msg += f"\n🔴 Протухшие/Заблокированные ({len(expired)}):\n" + "\n".join(expired[:10]) + "\n"
    if other:
        msg += f"\n🟡 Другие ({len(other)}):\n" + "\n".join(other[:5]) + "\n"
    # Add inline keyboard for re-login of expired accounts
    if expired:
        expired_names = [a["name"] for a in accounts if a.get("web_upload_login_status") in ("incorrect_credentials", "consent_failed", "manual_required", "suspended", "browser_internal_error")]
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"🔐 Перезалогинить {len(expired_names)} протухших", callback_data="login_expired")],
        ])
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=keyboard)
    else:
        await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_stop(update: Update, ctx):
    async with aiohttp.ClientSession() as session:
        data = await api_post(session, "/api/ig-web-upload/stop")
    if data.get("ok"):
        await update.message.reply_text("🛑 Все процессы остановлены")
    else:
        await update.message.reply_text(f"❌ {data.get('error', 'ошибка')}")

# ─── Callback Handler ─────────────────────────────────────────────────────────

async def callback_handler(update: Update, ctx):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "cancel":
        await query.edit_message_text("Отменено")
        return

    async with aiohttp.ClientSession() as session:
        if data.startswith("upload:"):
            name = data.split(":", 1)[1]
            await query.edit_message_text(f"🚀 Залив @{name}...")
            result = await api_post(session, "/api/ig-web-upload/start", {
                "accounts": [name], "engine": "clean_web", "browser_parallel": 1,
                "target": 3, "pre_warmup_min": 1, "pre_warmup_max": 2,
                "post_warmup_min": 1, "post_warmup_max": 3, "cooldown_hours": 4,
            })
        elif data == "upload_all":
            await query.edit_message_text("🚀 Залив всех готовых аккаунтов...")
            overview = await api_get(session, "/api/ig-web-upload/overview")
            names = [a["name"] for a in overview.get("accounts", []) if a.get("web_upload_login_status") == "logged_in"]
            if not names:
                await query.edit_message_text("Нет готовых аккаунтов")
                return
            result = await api_post(session, "/api/ig-web-upload/start", {
                "accounts": names, "engine": "clean_web", "browser_parallel": 5,
                "target": 3, "pre_warmup_min": 1, "pre_warmup_max": 2,
                "post_warmup_min": 1, "post_warmup_max": 3, "cooldown_hours": 4,
            })
        elif data.startswith("stories:"):
            name = data.split(":", 1)[1]
            await query.edit_message_text(f"📸 История @{name}...")
            result = await api_post(session, "/api/ig-web-upload/post-story", {"accounts": [name]})
        elif data == "stories_all":
            await query.edit_message_text("📸 Истории на всех...")
            overview = await api_get(session, "/api/ig-web-upload/overview")
            names = [a["name"] for a in overview.get("accounts", []) if a.get("web_upload_login_status") == "logged_in"]
            result = await api_post(session, "/api/ig-web-upload/post-story", {"accounts": names})
        elif data.startswith("login:"):
            name = data.split(":", 1)[1]
            await query.edit_message_text(f"🔐 Логин @{name}...")
            result = await api_post(session, "/api/ig-web-upload/workflow", {"task": "auto_login", "accounts": [name]})
        elif data == "login_all":
            await query.edit_message_text("🔐 Логин всех...")
            overview = await api_get(session, "/api/ig-web-upload/overview")
            names = [a["name"] for a in overview.get("accounts", [])]
            result = await api_post(session, "/api/ig-web-upload/workflow", {"task": "auto_login", "accounts": names})
        elif data == "login_expired":
            await query.edit_message_text("🔐 Перезалогинить протухших...")
            overview = await api_get(session, "/api/ig-web-upload/overview")
            names = [a["name"] for a in overview.get("accounts", []) if a.get("web_upload_login_status") in ("incorrect_credentials", "consent_failed", "manual_required", "suspended", "browser_internal_error", "unknown", "")]
            if not names:
                await query.message.reply_text("Нет протухших аккаунтов")
                return
            result = await api_post(session, "/api/ig-web-upload/workflow", {"task": "auto_login", "accounts": names})
        else:
            return

    if result.get("ok"):
        run_id = result.get("run_id", "")
        await query.message.reply_text(f"✅ Запущено! run_id={run_id}")
    else:
        await query.message.reply_text(f"❌ {result.get('error', 'ошибка')}")

# ─── Background: Story Trigger Notifications ──────────────────────────────────

last_seen_trigger_id = 0

async def background_check_triggers(ctx: ContextTypes.DEFAULT_TYPE):
    """Check for new story triggers every 10 min."""
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
                try:
                    await ctx.bot.send_message(chat_id=CHAT_ID, text=msg)
                except Exception:
                    pass
            last_seen_trigger_id = max(last_seen_trigger_id, tid)
    except Exception as e:
        logger.warning(f"trigger check error: {e}")

async def background_hourly_summary(ctx: ContextTypes.DEFAULT_TYPE):
    """Send hourly metrics summary."""
    try:
        async with aiohttp.ClientSession() as session:
            data = await api_get(session, "/api/ig-web-upload/metrics/overview?hours=24")
        if not data.get("ok"):
            return
        t = data.get("total", {})
        dl = data.get("delta_24h", {})
        msg = (
            f"📊 *Часовая сводка*\n"
            f"Подписчики: {fmt(t.get('followers',0))} ({fmt_delta(dl.get('followers',0))})\n"
            f"Просмотры: {fmt(t.get('views',0))} ({fmt_delta(dl.get('views',0))})\n"
            f"Лайки: {fmt(t.get('likes',0))} ({fmt_delta(dl.get('likes',0))})"
        )
        await ctx.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
    except Exception as e:
        logger.warning(f"hourly summary error: {e}")

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
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CallbackQueryHandler(callback_handler))

    # Background jobs
    job_queue = app.job_queue
    if job_queue:
        job_queue.run_repeating(background_check_triggers, interval=600, first=30)
        job_queue.run_repeating(background_hourly_summary, interval=3600, first=60)

    logger.info("Бот запущен")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


def run_forever():
    """Run bot with auto-restart on crash. Never dies."""
    while True:
        try:
            logger.info("Запуск бота...")
            main()
        except KeyboardInterrupt:
            logger.info("Остановка по запросу")
            break
        except Exception as e:
            logger.error(f"Бот упал: {e}. Перезапуск через 10 сек...")
            import time
            time.sleep(10)


if __name__ == "__main__":
    run_forever()
