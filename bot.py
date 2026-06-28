import os
import re
import logging
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bot")

FRANKFURTER_API = "https://api.frankfurter.app/latest"
COMMON = ["USD", "EUR", "GBP", "JPY", "CNY", "AUD", "CAD", "CHF", "INR", "BRL", "NGN", "ZAR"]

# 1-hour in-memory cache so we don't hammer the API
_rate_cache = {}
CACHE_TTL = 3600

# ============================================================
# Health server
# ============================================================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.send_header("Content-Type", "text/plain"); self.end_headers()
        self.wfile.write(b"Currency Bot alive.")
    def do_HEAD(self):
        self.send_response(200); self.send_header("Content-Type", "text/plain"); self.end_headers()
    def log_message(self, format, *args):
        return

def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()

# ============================================================
# Currency logic
# ============================================================
async def get_rate(from_cur: str, to_cur: str) -> float:
    key = f"{from_cur}_{to_cur}"
    now = datetime.now().timestamp()
    if key in _rate_cache:
        rate, ts = _rate_cache[key]
        if now - ts < CACHE_TTL:
            return rate
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(FRANKFURTER_API, params={"from": from_cur, "to": to_cur})
        if resp.status_code != 200:
            raise RuntimeError(f"API error {resp.status_code}")
        data = resp.json()
        rate = data.get("rates", {}).get(to_cur)
        if rate is None:
            raise ValueError(f"Currency {to_cur} not supported")
        _rate_cache[key] = (rate, now)
        return rate

def parse_query(text: str):
    """Parse '100 USD to EUR' / '100 USD EUR' / 'USD EUR' (1 unit)."""
    text = text.upper().replace(",", "").strip()
    m = re.match(r"^([\d.]+)\s+([A-Z]{3})\s+(?:TO\s+)?([A-Z]{3})$", text)
    if m:
        return float(m.group(1)), m.group(2), m.group(3)
    m = re.match(r"^([A-Z]{3})\s+(?:TO\s+)?([A-Z]{3})$", text)
    if m:
        return 1.0, m.group(1), m.group(2)
    raise ValueError("Format: `<amount> <FROM> to <TO>`\nExample: `100 USD to EUR`")

# ============================================================
# Menus
# ============================================================
def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💱 Quick Convert", callback_data="quick"),
         InlineKeyboardButton("📊 Popular Rates", callback_data="rates")],
        [InlineKeyboardButton("ℹ️ How to use", callback_data="help")],
    ])

def back_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="home")]])

# ============================================================
# Handlers
# ============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💱 *Currency Converter*\n\n"
        "Send me a conversion in this format:\n"
        "`100 USD to EUR`\n\n"
        "Or just two currencies (1 unit):\n"
        "`USD JPY`\n\n"
        "_Rates from frankfurter.app, updated daily._",
        reply_markup=main_menu(),
        parse_mode="Markdown",
    )

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "home":
        await query.edit_message_text(
            "💱 *Currency Converter*\n\nSend me a conversion like `100 USD to EUR`",
            reply_markup=main_menu(),
            parse_mode="Markdown",
        )
        return

    if data == "quick":
        await query.edit_message_text(
            "💱 *Quick Convert*\n\nSend me your conversion:\n"
            "`100 USD to EUR`\n"
            "`50 GBP JPY`\n"
            "`USD CNY`",
            reply_markup=back_kb(),
            parse_mode="Markdown",
        )
        return

    if data == "rates":
        await query.edit_message_text("📊 Loading popular rates…", reply_markup=back_kb())
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                pairs = ",".join(c for c in COMMON if c != "USD")
                resp = await client.get(FRANKFURTER_API, params={"from": "USD", "to": pairs})
                rates = resp.json().get("rates", {})
            lines = ["📊 *Rates for 1 USD:*\n"]
            for cur in COMMON:
                if cur == "USD":
                    continue
                r = rates.get(cur)
                if r:
                    lines.append(f"💵 1 USD = `{r:,.4f}` {cur}")
            await query.edit_message_text(
                "\n".join(lines) + "\n\n_Updated daily via frankfurter.app_",
                reply_markup=main_menu(),
                parse_mode="Markdown",
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Failed: {e}", reply_markup=main_menu())
        return

    if data == "help":
        await query.edit_message_text(
            "ℹ️ *How to use*\n\n"
            "Type your conversion in any of these formats:\n"
            "  • `100 USD to EUR`\n"
            "  • `100 USD EUR`\n"
            "  • `USD JPY` (uses 1 unit)\n\n"
            "Use any 3-letter ISO code: USD, EUR, GBP, NGN, BRL, JPY, CNY, INR, CAD, AUD, CHF, ZAR, and more.\n\n"
            "Rates updated daily from frankfurter.app.",
            reply_markup=main_menu(),
            parse_mode="Markdown",
        )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    try:
        amount, from_cur, to_cur = parse_query(text)
    except ValueError as e:
        await update.message.reply_text(f"⚠️ {e}", parse_mode="Markdown")
        return

    if from_cur == to_cur:
        await update.message.reply_text(
            f"💱 `{amount:,.2f} {from_cur}` = `{amount:,.2f} {to_cur}`\n_(Same currency.)_",
            reply_markup=main_menu(),
            parse_mode="Markdown",
        )
        return

    try:
        rate = await get_rate(from_cur, to_cur)
    except Exception as e:
        await update.message.reply_text(
            f"❌ Couldn't convert: {e}\n\nUse 3-letter codes like USD, EUR, GBP, JPY, NGN…",
            reply_markup=main_menu(),
        )
        return

    result = amount * rate
    await update.message.reply_text(
        f"💱 *Conversion*\n\n"
        f"`{amount:,.2f} {from_cur}` = `{result:,.4f} {to_cur}`\n\n"
        f"_1 {from_cur} = {rate:,.6f} {to_cur}_\n"
        f"_Updated daily via frankfurter.app_",
        reply_markup=main_menu(),
        parse_mode="Markdown",
    )

# ============================================================
# Main
# ============================================================
def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        log.critical("BOT_TOKEN env var missing!")
        return

    threading.Thread(target=run_health_server, daemon=True).start()

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(menu_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("Currency Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
