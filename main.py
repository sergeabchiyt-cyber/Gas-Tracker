"""
Fuel & Gas Price Tracker — Telegram Bot
Render Free Tier | Flask + APScheduler + python-telegram-bot v20+
"""

import asyncio
import logging
import os
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import yfinance as yf
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
PORT: int = int(os.environ.get("PORT", 8080))

BEIRUT_TZ = ZoneInfo("Asia/Beirut")

TICKERS: dict[str, str] = {
    "NG=F": "Natural Gas",
    "RB=F": "Gasoline (RBOB)",
    "HO=F": "Heating Oil",
    "BZ=F": "Brent Crude",
}

UNITS: dict[str, str] = {
    "NG=F": "MMBtu",
    "RB=F": "gallon",
    "HO=F": "gallon",
    "BZ=F": "barrel",
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

def fetch_prices() -> dict[str, dict]:
    results: dict[str, dict] = {}

    for ticker, label in TICKERS.items():
        try:
            data = yf.Ticker(ticker)
            info = data.fast_info

            price = getattr(info, "last_price", None)
            prev_close = getattr(info, "previous_close", None)

            if price is None:
                hist = data.history(period="2d")
                if not hist.empty:
                    price = float(hist["Close"].iloc[-1])
                    prev_close = float(hist["Close"].iloc[-2]) if len(hist) > 1 else None

            if price is None:
                results[ticker] = {"label": label, "error": "No data returned"}
                continue

            change = price - prev_close if prev_close else None
            pct_change = (change / prev_close * 100) if prev_close else None

            results[ticker] = {
                "label": label,
                "price": round(price, 4),
                "prev_close": round(prev_close, 4) if prev_close else None,
                "change": round(change, 4) if change is not None else None,
                "pct_change": round(pct_change, 2) if pct_change is not None else None,
                "unit": UNITS[ticker],
                "error": None,
            }

        except Exception as exc:
            logger.error("Failed to fetch %s: %s", ticker, exc)
            results[ticker] = {"label": label, "error": str(exc)}

    return results


# ─── Message Builder ─────────────────────────────────────────────────────────

def build_report(prices: dict[str, dict], title: str = "Daily Fuel & Gas Report") -> str:
    now = datetime.now(BEIRUT_TZ).strftime("%Y-%m-%d %H:%M %Z")
    lines = [f"*{title}*", f"_{now}_", ""]

    for ticker, data in prices.items():
        if data.get("error"):
            lines.append(f"* *{data['label']}* (`{ticker}`): {data['error']}")
            continue

        arrow = "🟢" if (data["change"] or 0) >= 0 else "🔴"
        sign = "+" if (data["change"] or 0) >= 0 else ""

        price_str = f"${data['price']:.4f}/{data['unit']}"
        change_str = ""
        if data["change"] is not None:
            change_str = (
                f"  {arrow} {sign}{data['change']:.4f} "
                f"({sign}{data['pct_change']:.2f}%)"
            )

        lines.append(f"* *{data['label']}* (`{ticker}`)")
        lines.append(f"  Price: `{price_str}`{change_str}")
        lines.append("")

    lines.append("_Source: Yahoo Finance via yfinance_")
    return "\n".join(lines)


# ─── Telegram Handlers ───────────────────────────────────────────────────────

async def cmd_start(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "⛽ *Fuel & Gas Price Tracker*\n\n"
        "Commands:\n"
        "/prices — Fetch current prices\n"
        "/start — Show this message",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_prices(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Fetching prices...", parse_mode=ParseMode.MARKDOWN)
    prices = fetch_prices()
    report = build_report(prices, title="Current Fuel & Gas Prices")
    await update.message.reply_text(report, parse_mode=ParseMode.MARKDOWN)


# ─── Scheduled Daily Report ──────────────────────────────────────────────────

async def send_daily_report(bot) -> None:
    logger.info("Sending scheduled daily report to chat %s", CHAT_ID)
    try:
        prices = fetch_prices()
        report = build_report(prices)
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
        await asyncio.Event().wait()  # block forever without signal handlers

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
