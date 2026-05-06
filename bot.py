import asyncio
import io
import logging
import os
from functools import partial

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from agent import analyze_morning_data
from broker import Broker
import config

load_dotenv()
logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)

ALLOWED_USER_ID = int(os.getenv("TELEGRAM_USER_ID") or "0")
CONVICTION_ICON = {"high": "🔥", "medium": "⚡", "low": "💧"}
SENTIMENT_ICON = {"bullish": "🟢", "neutral": "🟡", "bearish": "🔴"}


class TelegramImage:
    """Wraps a Telegram photo download so agent.py can treat it like a Streamlit UploadedFile."""
    def __init__(self, data: bytes):
        self._buf = io.BytesIO(data)
        self.type = "image/jpeg"

    def seek(self, pos):
        self._buf.seek(pos)

    def read(self):
        return self._buf.read()


def is_allowed(user_id: int) -> bool:
    return ALLOWED_USER_ID == 0 or user_id == ALLOWED_USER_ID


# ── Commands ───────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(
        f"AutoInvest Bot ready.\n\n"
        f"Your Telegram user ID: <code>{uid}</code>\n"
        f"Add <code>TELEGRAM_USER_ID={uid}</code> to your .env to lock the bot to only you.\n\n"
        "Send your morning notes (text and/or screenshots), then tap <b>Analyze</b>.",
        parse_mode="HTML",
    )


# ── Message handlers ───────────────────────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    context.user_data["text"] = update.message.text
    context.user_data.setdefault("photos", [])
    keyboard = [[InlineKeyboardButton("🔍 Analyze", callback_data="analyze")]]
    await update.message.reply_text(
        "Got text. Add screenshots or tap Analyze.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    context.user_data.setdefault("photos", [])
    context.user_data.setdefault("text", "")

    photo = update.message.photo[-1]
    context.user_data["photos"].append(photo.file_id)

    if update.message.caption:
        context.user_data["text"] = update.message.caption

    count = len(context.user_data["photos"])
    keyboard = [[InlineKeyboardButton("🔍 Analyze", callback_data="analyze")]]
    await update.message.reply_text(
        f"Got {count} screenshot(s). Add more or tap Analyze.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ── Analysis ───────────────────────────────────────────────────────────────────

async def run_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE) -> dict:
    text = context.user_data.get("text", "")
    photo_ids = context.user_data.get("photos", [])

    images = []
    for file_id in photo_ids:
        f = await context.bot.get_file(file_id)
        buf = io.BytesIO()
        await f.download_to_memory(buf)
        images.append(TelegramImage(buf.getvalue()))

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, partial(analyze_morning_data, text, images))

    context.user_data["text"] = ""
    context.user_data["photos"] = []
    context.user_data["analysis"] = result
    return result


# ── Callback handler ───────────────────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_allowed(update.effective_user.id):
        return

    data = query.data
    chat_id = update.effective_chat.id

    # ── Analyze ────────────────────────────────────────────────────────────────
    if data == "analyze":
        text = context.user_data.get("text", "")
        photos = context.user_data.get("photos", [])
        if not text and not photos:
            await query.edit_message_text("No data. Send text or screenshots first.")
            return

        await query.edit_message_text("🔍 Claude is analyzing your morning data...")

        try:
            result = await run_analysis(update, context)
        except Exception as e:
            await query.edit_message_text(f"Analysis error: {e}")
            return

        sentiment = result.get("market_sentiment", "neutral")
        icon = SENTIMENT_ICON.get(sentiment, "🟡")
        market_ctx = result.get("market_context", "")
        trades = result.get("trades", [])
        skipped = result.get("skipped", [])
        total = sum(config.POSITION_SIZES.get(t["conviction"], 0) for t in trades)

        await query.edit_message_text(
            f"{icon} <b>Market: {sentiment.upper()}</b>\n{market_ctx}"
            + (f"\n\n<b>{len(trades)} trade(s) — ${total:.0f} total</b>" if trades else "\n\nNo actionable trades."),
            parse_mode="HTML",
        )

        if not trades:
            return

        for trade in trades:
            ticker = trade["ticker"]
            conviction = trade["conviction"]
            notional = config.POSITION_SIZES.get(conviction, 100)
            ci = CONVICTION_ICON.get(conviction, "")
            name = trade.get("company_name", "")

            msg = (
                f"{ci} <b>{ticker}</b>" + (f" — {name}" if name else "") + "\n"
                f"<b>{conviction.upper()}</b> — ${notional:.0f}  |  "
                f"TP +{trade['take_profit_pct']:.1f}%  |  SL -{trade['stop_loss_pct']:.1f}%\n"
                f"🟢 {trade.get('green_flags', '?')} green  🔴 {trade.get('red_flags', '?')} red\n"
                f"<i>{trade['reasoning']}</i>"
            )
            if trade.get("notes"):
                msg += f"\n⚠️ <i>{trade['notes']}</i>"

            keyboard = [[
                InlineKeyboardButton("✅ Execute", callback_data=f"execute_{ticker}"),
                InlineKeyboardButton("❌ Skip", callback_data=f"skip_{ticker}"),
            ]]
            await context.bot.send_message(
                chat_id=chat_id,
                text=msg,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

        if skipped:
            lines = "\n".join(f"• <b>{s.get('ticker','?')}</b> — {s.get('reason','')}" for s in skipped)
            await context.bot.send_message(chat_id=chat_id, text=f"<b>Skipped:</b>\n{lines}", parse_mode="HTML")

    # ── Execute trade ──────────────────────────────────────────────────────────
    elif data.startswith("execute_"):
        ticker = data[len("execute_"):]
        analysis = context.user_data.get("analysis", {})
        trade = next((t for t in analysis.get("trades", []) if t["ticker"] == ticker), None)

        if not trade:
            await query.edit_message_reply_markup(None)
            await context.bot.send_message(chat_id, f"Trade data for {ticker} not found.")
            return

        await query.edit_message_reply_markup(None)
        status = await context.bot.send_message(chat_id, f"⏳ Placing order for {ticker}...")

        try:
            b = Broker(os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY"))
            notional = config.POSITION_SIZES.get(trade["conviction"], 100)
            order, error = b.place_bracket_order(
                ticker, notional, trade["take_profit_pct"], trade["stop_loss_pct"]
            )
        except Exception as e:
            await status.edit_text(f"❌ <b>{ticker}</b>: {e}", parse_mode="HTML")
            return

        if error:
            await status.edit_text(f"❌ <b>{ticker}</b>: {error}", parse_mode="HTML")
        elif order.get("fractional"):
            await status.edit_text(
                f"✅ <b>{ticker}</b>: {order['shares']:.4f} shares @ ~${order['entry_price']:.2f}\n"
                f"⚠️ Fractional — set TP ${order['take_profit_price']:.2f} / SL ${order['stop_loss_price']:.2f} manually\n"
                f"<code>{order['id']}</code>",
                parse_mode="HTML",
            )
        else:
            await status.edit_text(
                f"✅ <b>{ticker}</b>: {order['shares']} shares @ ~${order['entry_price']:.2f}\n"
                f"TP ${order['take_profit_price']:.2f}  |  SL ${order['stop_loss_price']:.2f}\n"
                f"<code>{order['id']}</code>",
                parse_mode="HTML",
            )

    # ── Skip trade ─────────────────────────────────────────────────────────────
    elif data.startswith("skip_"):
        ticker = data[len("skip_"):]
        await query.edit_message_reply_markup(None)
        await context.bot.send_message(chat_id, f"⏭️ Skipped {ticker}.")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN not set in .env")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
