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
    """
    Scrapes IPT Group Lebanon fuel prices via AUDITOR API.
    IPT site is server-rendered — js: False is sufficient.
    """
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
        logger.info("AUDITOR response received. Keys: %s", list(data.keys()) if isinstance(data, dict) else type(data))
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
    lines = [f"*{title}*", f"_{now}_", ""]

    if not result["success"]:
        lines.append(f"⚠️ Failed to fetch prices: {result['error']}")
        return "\n".join(lines)

    data = result["data"]

    # AUDITOR returns structured data — iterate whatever fields are present
    # Handles both list-of-items and dict-of-items response shapes
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        # Look for a nested list key (e.g. "results", "data", "items", "prices")
        items = None
        for key in ("results", "data", "items", "prices", "fuel_prices"):
            if key in data and isinstance(data[key], list):
                items = data[key]
                break
        if items is None:
            # Flat dict — treat each key/value as a price entry
            for k, v in data.items():
                lines.append(f"• *{k}*: `{v}`")
            lines.append("")
            lines.append("_Source: IPT Group Lebanon_")
            return "\n".join(lines)
    else:
        lines.append("⚠️ Unexpected response format from AUDITOR.")
        return "\n".join(lines)

    if not items:
        lines.append("⚠️ No price data found in response.")
        return "\n".join(lines)

    for item in items:
        if not isinstance(item, dict):
            continue

        # Flexibly extract label and price from whatever keys AUDITOR infers
        label = (
            item.get("fuel_type")
            or item.get("type")
            or item.get("name")
            or item.get("product")
            or item.get("label")
            or "Unknown"
        )
        price = (
            item.get("price")
            or item.get("value")
            or item.get("amount")
            or item.get("cost")
            or "N/A"
        )
        unit = item.get("unit") or item.get("currency") or "LBP"

        lines.append(f"• *{label}*: `{price} {unit}`")

    lines.append("")
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
