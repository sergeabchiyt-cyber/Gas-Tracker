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
CHAT_ID: str = os.environ.get("CHAT_ID", "")
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


# ─── Dynamic Field Detection ─────────────────────────────────────────────────

def detect_field(record: dict, candidates: list[str]) -> str | None:
    """Return the first key in record that contains any of the candidate substrings."""
    for key in record:
        for c in candidates:
            if c in key.lower():
                return key
    return None


def fuel_emoji(label: str) -> str:
    l = label.lower().replace(" ", "").replace("_", "")
    if "95" in l:
        return "⛽"
    if "98" in l:
        return "🔵"
    if "diesel" in l:
        return "🟡"
    if "lpg" in l or "gas" in l:
        return "🟠"
    return "🔹"


# ─── Message Builder ─────────────────────────────────────────────────────────

def build_report(result: dict, title: str = "IPT Fuel Prices — Lebanon") -> str:
    now = datetime.now(BEIRUT_TZ).strftime("%Y-%m-%d %H:%M %Z")

    if not result["success"]:
        return f"*{title}*\n_{now}_\n\n⚠️ {result['error']}"

    data = result["data"]

    # Pull records from `data` key — single source of truth
    records = data.get("data") if isinstance(data, dict) else None
    if not records or not isinstance(records, list):
        return f"*{title}*\n_{now}_\n\n⚠️ No records found in response."

    # De-duplicate: use fuel type field value as dedup key
    seen = set()
    unique_records = []
    for r in records:
        fuel_key = detect_field(r, ["fuel", "type", "product", "name", "label"])
        dedup_val = str(r.get(fuel_key, id(r))).lower() if fuel_key else str(id(r))
        if dedup_val not in seen:
            seen.add(dedup_val)
            unique_records.append(r)

    # Extract date from first record dynamically
    first = unique_records[0] if unique_records else {}
    date_key = detect_field(first, ["date", "time", "updated"])
    date_str = first.get(date_key, "N/A") if date_key else "N/A"

    lines = [
        f"*{title}*",
        f"📅 _{date_str}_",
        f"🕐 _{now}_",
        "─────────────────",
    ]

    # Skip these keys from per-record display — shown in header already
    skip_keys = {date_key, "currency", "date", "time", "updated_at"}

    for r in unique_records:
        fuel_key = detect_field(r, ["fuel", "type", "product", "name", "label"])
        price_key = detect_field(r, ["price", "cost", "value", "amount", "rate"])
        currency_key = detect_field(r, ["currency", "unit", "cur"])

        fuel_label = str(r.get(fuel_key, "Unknown")).replace("_", " ").upper() if fuel_key else "Unknown"
        price_val = str(r.get(price_key, "N/A")) if price_key else "N/A"
        currency_val = str(r.get(currency_key, "L.L.")) if currency_key else "L.L."

        emoji = fuel_emoji(fuel_label)
        lines.append(f"{emoji} *{fuel_label}*")
        lines.append(f"    `{price_val} {currency_val}`")

        # Append any extra fields that aren't already shown
        shown = {fuel_key, price_key, currency_key} | skip_keys
        for k, v in r.items():
            if k not in shown and v not in (None, "", "N/A"):
                lines.append(f"    _{k}: {v}_")

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
        if CHAT_ID:
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
            logger.info("Scheduler started. Daily report scheduled at 00:00 Asia/Beirut (UTC+2).")
        else:
            logger.warning("CHAT_ID not set — scheduled daily report disabled.")
        scheduler.start()

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
