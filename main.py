"""
IPT Fuel Price Tracker — Telegram Bot
Render Free Tier | Flask + APScheduler + python-telegram-bot v20+
Data Source: IPT Group Lebanon via AUDITOR /api/raw + BeautifulSoup (no AI)
"""

import asyncio
import logging
import os
import re
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
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

BOT_TOKEN:       str = os.environ["BOT_TOKEN"]
CHAT_ID:         str = os.environ.get("CHAT_ID", "")
AUDITOR_API_KEY: str = os.environ["AUDITOR_API_KEY"]
PORT:            int = int(os.environ.get("PORT", 8080))

BEIRUT_TZ = ZoneInfo("Asia/Beirut")

AUDITOR_RAW_ENDPOINT = "https://web-scraping-production.up.railway.app/api/raw"
IPT_URL              = "https://www.iptgroup.com.lb/ipt/en/our-stations/fuel-prices"

FUEL_EMOJIS = {
    "95":     "⛽",
    "98":     "🔵",
    "diesel": "🟡",
    "lpg":    "🟠",
    "gas":    "🟠",
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


# ─── Raw Fetcher ─────────────────────────────────────────────────────────────

def fetch_raw_html() -> dict:
    """
    Calls AUDITOR /api/raw — returns preprocessed, table-isolated HTML.
    No AI involved. Zero token cost.
    """
    try:
        response = requests.post(
            AUDITOR_RAW_ENDPOINT,
            headers={
                "X-Api-Key":    AUDITOR_API_KEY,
                "Content-Type": "application/json",
            },
            json={"url": IPT_URL, "js": False, "adaptive": True},
            timeout=60,
        )
        response.raise_for_status()
        data = response.json()
        content = data.get("content", "")
        if not content:
            return {"success": False, "error": "Empty content in AUDITOR response"}
        logger.info(
            "AUDITOR /api/raw OK — %d isolated chars in %ss",
            data.get("isolated_chars", 0),
            data.get("elapsed", "?"),
        )
        return {"success": True, "content": content}

    except requests.exceptions.Timeout:
        logger.error("AUDITOR /api/raw timed out.")
        return {"success": False, "error": "Request timed out after 60s"}
    except requests.exceptions.HTTPError as exc:
        logger.error("AUDITOR HTTP error: %s", exc)
        return {"success": False, "error": f"HTTP {exc.response.status_code}"}
    except Exception as exc:
        logger.error("AUDITOR fetch failed: %s", exc)
        return {"success": False, "error": str(exc)}


# ─── HTML Parser ─────────────────────────────────────────────────────────────

NUMBER_RE = re.compile(r"[\d,]+(?:\.\d+)?")


def fuel_emoji(label: str) -> str:
    l = label.lower()
    for key, emoji in FUEL_EMOJIS.items():
        if key in l:
            return emoji
    return "🔹"


def parse_fuel_table(html: str) -> list[dict]:
    """
    Multi-strategy BeautifulSoup parser.
    Strategy 1: <table> with rows where one cell is a label, another is a price.
    Strategy 2: Any element containing a price pattern adjacent to a fuel name.
    Strategy 3: Broad text scan — find lines that match 'FUEL_NAME ... NUMBER ... L.L.'
    Returns list of {label, price, currency} dicts.
    """
    soup    = BeautifulSoup(html, "html.parser")
    results = []

    # ── Strategy 1: Table rows ───────────────────────────────────────────────
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        for row in rows:
            cells = [c.get_text(separator=" ", strip=True) for c in row.find_all(["td", "th"])]
            if len(cells) < 2:
                continue
            label_cell = cells[0]
            # Find first cell that looks like a price
            price_cell = next(
                (c for c in cells[1:] if NUMBER_RE.search(c) and len(c) < 60),
                None,
            )
            if not price_cell:
                continue
            label = label_cell.strip()
            if not label or len(label) > 60:
                continue
            # Filter out header-looking rows
            if label.lower() in ("fuel", "type", "price", "product", "name", "label"):
                continue
            price_match = NUMBER_RE.search(price_cell)
            price = price_match.group(0) if price_match else price_cell.strip()
            currency = "L.L." if re.search(r"L\.?L\.?", price_cell, re.IGNORECASE) else ""
            results.append({"label": label, "price": price, "currency": currency})

    if results:
        logger.info("Strategy 1 (table) found %d records", len(results))
        return _deduplicate(results)

    # ── Strategy 2: Paired siblings / parent containers ──────────────────────
    price_pattern = re.compile(r"\b\d[\d,]*(?:\.\d+)?\s*(L\.?L\.?|USD|\$)", re.IGNORECASE)
    for el in soup.find_all(True):
        text = el.get_text(separator=" ", strip=True)
        if not price_pattern.search(text):
            continue
        if len(el.find_all(True)) > 15:  # skip large containers
            continue
        children = [c for c in el.children if hasattr(c, "get_text")]
        if len(children) >= 2:
            label = children[0].get_text(strip=True)
            price_text = " ".join(c.get_text(strip=True) for c in children[1:])
            if label and len(label) < 60 and price_pattern.search(price_text):
                m = NUMBER_RE.search(price_text)
                price = m.group(0) if m else price_text
                currency = "L.L." if re.search(r"L\.?L\.?", price_text, re.IGNORECASE) else ""
                results.append({"label": label, "price": price, "currency": currency})

    if results:
        logger.info("Strategy 2 (siblings) found %d records", len(results))
        return _deduplicate(results)

    # ── Strategy 3: Full-text line scan ──────────────────────────────────────
    full_text = soup.get_text(separator="\n")
    line_re   = re.compile(
        r"([A-Za-z][A-Za-z0-9 /()]{1,40})\s+(\d[\d,]*(?:\.\d+)?)\s*(L\.?L\.?|USD|\$)?",
        re.IGNORECASE,
    )
    for line in full_text.splitlines():
        m = line_re.search(line)
        if m:
            results.append({
                "label":    m.group(1).strip(),
                "price":    m.group(2).strip(),
                "currency": m.group(3).strip() if m.group(3) else "L.L.",
            })

    if results:
        logger.info("Strategy 3 (text scan) found %d records", len(results))

    return _deduplicate(results)


def _deduplicate(records: list[dict]) -> list[dict]:
    """Remove duplicate labels (case-insensitive, keep first occurrence)."""
    seen   = set()
    unique = []
    for r in records:
        key = r.get("label", "").lower().strip()
        if key and key not in seen:
            seen.add(key)
            unique.append(r)
    return unique


# ─── Message Builder ─────────────────────────────────────────────────────────

def build_report(records: list[dict] | None, error: str | None,
                 title: str = "IPT Fuel Prices — Lebanon") -> str:
    now = datetime.now(BEIRUT_TZ).strftime("%Y-%m-%d %H:%M %Z")

    if error:
        return f"*{title}*\n_{now}_\n\n⚠️ {error}"

    if not records:
        return f"*{title}*\n_{now}_\n\n⚠️ No price data found."

    lines = [
        f"*{title}*",
        f"🕐 _{now}_",
        "─────────────────",
    ]

    for r in records:
        label    = r.get("label", "Unknown")
        price    = r.get("price", "N/A")
        currency = r.get("currency", "L.L.")
        emoji    = fuel_emoji(label)
        lines.append(f"{emoji} *{label}*")
        lines.append(f"    `{price} {currency}`")

    lines.append("─────────────────")
    lines.append("_Source: IPT Group Lebanon_")
    return "\n".join(lines)


# ─── Price Fetch + Parse ─────────────────────────────────────────────────────

def fetch_prices() -> tuple[list[dict] | None, str | None]:
    result = fetch_raw_html()
    if not result["success"]:
        return None, result["error"]
    records = parse_fuel_table(result["content"])
    if not records:
        return None, "Parser found no price records in the preprocessed HTML."
    return records, None


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
    records, error = fetch_prices()
    report = build_report(records, error, title="Current IPT Fuel Prices — Lebanon")
    await update.message.reply_text(report, parse_mode=ParseMode.MARKDOWN)


# ─── Scheduled Daily Report ──────────────────────────────────────────────────

async def send_daily_report(bot) -> None:
    logger.info("Sending scheduled daily report to chat %s", CHAT_ID)
    try:
        records, error = fetch_prices()
        report = build_report(records, error)
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
        application = Application.builder().token(BOT_TOKEN).build()
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
            logger.info("Scheduler: daily report at 00:00 Asia/Beirut.")
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


start_bot_thread()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
