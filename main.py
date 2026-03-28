"""
Fuel & Gas Price Tracker — Telegram Bot
Render Free Tier | Flask + APScheduler + python-telegram-bot v20+
Data Source: IPT Group Lebanon via AUDITOR scraping API
"""

import asyncio
import logging
import os
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from flask import Flask, jsonify
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

# ─── Logging ────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ─── Config ─────────────────────────────────────────────────────────────────

BOT_TOKEN: str = os.environ["BOT_TOKEN"]
CHAT_ID: str = os.environ["CHAT_ID"]
AUDITOR_API_KEY: str = os.environ["AUDITOR_API_KEY"]
PORT: int = int(os.environ.get("PORT", 8080))

BEIRUT_TZ = ZoneInfo("Asia/Beirut")

AUDITOR_ENDPOINT = "https://web-scraping-production.up.railway.app/api/scrape"
IPT_URL = "https://www.iptgroup.com.lb/ipt/en/our-stations/fuel-prices"

FUEL_LABELS = {
    "unl_95":  ("⛽", "Unleaded 95"),
    "unl_98":  ("🔵", "Unleaded 98"),
    "diesel":  ("🟡", "Diesel"),
    "gas_lpg": ("🟠", "Gas / LPG"),
}

# ─── Flask App ───────────────────────────────────────────────────────────────

app = Flask(__name__)


@app.route("/")
def index():
    return jsonify({"status": "ok", "service": "fuel-tracker-bot"}), 200


@app.route("/status")
def status():
    now_beirut = datetime.now(BEIRUT_TZ).isoformat()
    return jsonify({"status": "ok", "time_beirut": now_beirut}), 200


# ─── Price Fetcher ───────────────────────────────────────────────────────────

def fetch_prices() -> dict:
    try:
        response = requests.post(
            AUDITOR_ENDPOINT,
            headers={
                "X-Api-Key": AUDITOR_API_KEY,
                "Content-Type": "application/json",
            },
            json={
                "url": IPT_URL,
                "js": False,
                "adaptive": True,
            },
            timeout=60,
        )
        response.raise_for_status()
        data = response.json()
        logger.info("AUDITOR response received.")
        return {"success": True, "data": data}

    except requests.exceptions.Timeout:
        logger.error("AUDITOR request timed out.")
        return {"success": False, "error": "Request timed out after 60s"}
    except requests.exceptions.HTTPError as exc:
        logger.error("AUDITOR HTTP error: %s", exc)
        return {"success": False, "error": f"HTTP {exc.response.status_code}"}
    except Exception as exc:
        logger.error("AUDITOR fetch failed: %s", exc)
        return {"success": False, "error": str(exc)}


# ─── Message Builder ─────────────────────────────────────────────────────────

def build_report(result: dict, title: str = "IPT Fuel Prices — Lebanon") -> str:
    now = datetime.now(BEIRUT_TZ).strftime("%Y-%m-%d %H:%M %Z")

    if not result["success"]:
        return f"*{title}*\n_{now}_\n\n⚠️ {result['error']}"

    data = result["data"]

    # Pull records from `data` key only — single source of truth, no duplication
    records = data.get("data") if isinstance(data, dict) else None
    if not records or not isinstance(records, list):
        return f"*{title}*\n_{now}_\n\n⚠️ No records found in response."

    # De-duplicate by fuel_type — keep first occurrence
    seen = set()
    unique_records = []
    for r in records:
        ft = r.get("fuel_type", "").lower()
        if ft and ft not in seen:
            seen.add(ft)
            unique_records.append(r)

    date_str = unique_records[0].get("date", "N/A") if unique_records else "N/A"

    lines = [
        f"*{title}*",
        f"📅 _{date_str}_",
        f"🕐 _{now}_",
        "─────────────────",
    ]

    for r in unique_records:
        ft = r.get("fuel_type", "unknown").lower()
        price = r.get("price_ll", "N/A")
        currency = r.get("currency", "L.L.")
        emoji, label = FUEL_LABELS.get(ft, ("🔹", ft.upper()))
        lines.append(f"{emoji} *{label}*")
        lines.append(f"    `{price} {currency}`")

    lines.append("─────────────────")
    lines.append("_Source: IPT Group Lebanon_")
    return "\n".join(lines)


# ─── Telegram Handlers ───────────────────────────────────────────────────────

async def cmd_start(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "⛽ *IPT Fuel Price Tracker — Lebanon*\n\n"
        "Commands:\n"
        "/prices — Fetch current IPT fuel prices\n"
        "/start — Show this message",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_prices(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Fetching prices...", parse_mode=ParseMode.MARKDOWN)
    result = fetch_prices()
    report = build_report(result, title="Current IPT Fuel Prices — Lebanon")
    await update.message.reply_text(report, parse_mode=ParseMode.MARKDOWN)


# ─── Scheduled Daily Report ──────────────────────────────────────────────────

async def send_daily_report(bot) -> None:
    logger.info("Sending scheduled daily report to chat %s", CHAT_ID)
    try:
        result = fetch_prices()
        report = build_report(result)
        await bot.send_message(
            chat_id=CHAT_ID,
            text=report,
            parse_mode=ParseMode.MARKDOWN,
        )
        logger.info("Daily report sent successfully.")
    except Exception as exc:
        logger.error("Failed to send daily report: %s", exc)


# ─── Bot + Scheduler Runner ──────────────────────────────────────────────────

def run_bot() -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _run():
        application = (
            Application.builder()
            .token(BOT_TOKEN)
            .build()
        )

        application.add_handler(CommandHandler("start", cmd_start))
        application.add_handler(CommandHandler("prices", cmd_prices))

        scheduler = AsyncIOScheduler(timezone=BEIRUT_TZ)
        scheduler.add_job(
            send_daily_report,
            trigger="cron",
            hour=0,
            minute=0,
            args=[application.bot],
            id="daily_fuel_report",
            replace_existing=True,
            misfire_grace_time=300,
        )
        scheduler.start()
        logger.info("Scheduler started. Daily report scheduled at 00:00 Asia/Beirut (UTC+2).")

        await application.initialize()
        await application.start()
        await application.updater.start_polling(
            allowed_updates=["message"],
            drop_pending_updates=True,
        )

        logger.info("Bot polling started.")
        await asyncio.Event().wait()

    loop.run_until_complete(_run())


# ─── Entry Point ─────────────────────────────────────────────────────────────

def start_bot_thread() -> None:
    bot_thread = threading.Thread(target=run_bot, name="TelegramBotThread", daemon=True)
    bot_thread.start()
    logger.info("Telegram bot thread started.")


# Triggered at gunicorn import time
start_bot_thread()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
