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
from earnings import earnings_warning
from history import load_history, save_history, add_today, build_context_summary, get_ticker_history, build_history_report
from spend_tracker import get_remaining, get_today_spent, record_spend, DAILY_LIMIT
import config

load_dotenv()
logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)

ALLOWED_USER_ID = int(os.getenv("TELEGRAM_USER_ID") or "0")
CONVICTION_ICON = {"high": "🔥", "medium": "⚡", "low": "💧"}
SENTIMENT_ICON = {"bullish": "🟢", "neutral": "🟡", "bearish": "🔴"}


def select_trades(trades: list, budget: float) -> list:
    """Pick subset of trades that maximizes spend without exceeding budget.
    Prefers higher conviction on ties (sort ensures high-conviction items placed first in DP)."""
    order = {"high": 0, "medium": 1, "low": 2}
    sorted_trades = sorted(trades, key=lambda t: order.get(t["conviction"], 3))
    costs = [int(config.POSITION_SIZES.get(t["conviction"], 100)) for t in sorted_trades]
    n = len(sorted_trades)
    cap = int(budget)

    dp = [[0] * (cap + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        c = costs[i - 1]
        for j in range(cap + 1):
            dp[i][j] = dp[i - 1][j]
            if j >= c:
                dp[i][j] = max(dp[i][j], dp[i - 1][j - c] + c)

    selected = []
    j = cap
    for i in range(n, 0, -1):
        if dp[i][j] != dp[i - 1][j]:
            selected.append(sorted_trades[i - 1])
            j -= costs[i - 1]

    return selected


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


async def cmd_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    try:
        b = Broker(os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY"))
        acc = await asyncio.get_event_loop().run_in_executor(None, b.get_account)
        market_open = await asyncio.get_event_loop().run_in_executor(None, b.is_market_open)
        status = "🟢 Open" if market_open else "🔴 Closed"
        await update.message.reply_text(
            f"📊 <b>Alpaca Account</b>\n"
            f"Portfolio: ${acc['portfolio_value']:,.2f}\n"
            f"Cash: ${acc['cash']:,.2f}\n"
            f"Buying Power: ${acc['buying_power']:,.2f}\n"
            f"Market: {status}",
            parse_mode="HTML",
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error fetching account: {e}")


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    history = await asyncio.get_event_loop().run_in_executor(None, load_history)
    report = build_history_report(history)
    await update.message.reply_text(report, parse_mode="HTML")


async def cmd_budget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    spent = get_today_spent()
    remaining = get_remaining()
    await update.message.reply_text(
        f"💰 <b>Daily Budget</b>\n"
        f"Spent: ${spent:.0f} / ${DAILY_LIMIT:.0f}\n"
        f"Remaining: ${remaining:.0f}",
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

    history = load_history()
    history_context = build_context_summary(history)

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None, partial(analyze_morning_data, text, images, history_context)
    )

    trades = result.get("trades", [])
    skipped = result.get("skipped", [])
    sentiment = result.get("market_sentiment", "neutral")
    updated = add_today(history, trades, skipped, sentiment)
    save_history(updated)

    context.user_data["text"] = ""
    context.user_data["photos"] = []
    context.user_data["analysis"] = result
    context.user_data["history"] = updated
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
        all_trades = result.get("trades", [])
        skipped = result.get("skipped", [])

        remaining = get_remaining()
        trades = select_trades(all_trades, remaining)
        # Trades excluded by budget go to skipped display
        excluded = [t for t in all_trades if t not in trades]

        total = sum(config.POSITION_SIZES.get(t["conviction"], 0) for t in trades)
        spent = get_today_spent()

        await query.edit_message_text(
            f"{icon} <b>Market: {sentiment.upper()}</b>\n{market_ctx}\n\n"
            f"💰 Budget: ${spent:.0f} spent · ${remaining:.0f} remaining\n"
            + (f"<b>{len(trades)} trade(s) — ${total:.0f} of ${remaining:.0f}</b>" if trades else "<b>No actionable trades.</b>"),
            parse_mode="HTML",
        )

        if not trades:
            return

        history = context.user_data.get("history", load_history())

        # Fetch earnings warnings in parallel
        loop = asyncio.get_event_loop()
        earnings_warnings = await asyncio.gather(*[
            loop.run_in_executor(None, earnings_warning, t["ticker"])
            for t in trades
        ])
        earnings_map = {t["ticker"]: w for t, w in zip(trades, earnings_warnings)}

        for trade in trades:
            ticker = trade["ticker"]
            conviction = trade["conviction"]
            notional = config.POSITION_SIZES.get(conviction, 100)
            ci = CONVICTION_ICON.get(conviction, "")
            name = trade.get("company_name", "")

            th = get_ticker_history(history, ticker)
            bull = th["bullish_days"]
            total_h = th["total_days"]
            if total_h == 0:
                history_badge = "📅 First mention today"
            elif bull == total_h:
                history_badge = f"📈 Bullish {bull}/{total_h} days ↑"
            elif bull == 0:
                history_badge = f"📉 Bearish all {total_h} days ↓"
            else:
                history_badge = f"📊 Bullish {bull}/{total_h} days"

            msg = (
                f"{ci} <b>{ticker}</b>" + (f" — {name}" if name else "") + "\n"
                f"<b>{conviction.upper()}</b> — ${notional:.0f}  |  "
                f"TP +{trade['take_profit_pct']:.1f}%  |  SL -{trade['stop_loss_pct']:.1f}%\n"
                f"🟢 {trade.get('green_flags', '?')} green  🔴 {trade.get('red_flags', '?')} red\n"
                f"{history_badge}\n"
                f"<i>{trade['reasoning']}</i>"
            )
            if earnings_map.get(ticker):
                msg += f"\n{earnings_map[ticker]}"
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

        skip_lines = [f"• <b>{s.get('ticker','?')}</b> — {s.get('reason','')}" for s in skipped]
        skip_lines += [f"• <b>{t['ticker']}</b> — over daily budget" for t in excluded]
        if skip_lines:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"<b>Skipped:</b>\n" + "\n".join(skip_lines),
                parse_mode="HTML",
            )

    # ── Execute trade ──────────────────────────────────────────────────────────
    elif data.startswith("execute_"):
        ticker = data[len("execute_"):]
        analysis = context.user_data.get("analysis", {})
        trade = next((t for t in analysis.get("trades", []) if t["ticker"] == ticker), None)

        if not trade:
            await query.edit_message_reply_markup(None)
            await context.bot.send_message(chat_id, f"Trade data for {ticker} not found.")
            return

        notional = config.POSITION_SIZES.get(trade["conviction"], 100)
        remaining = get_remaining()
        if notional > remaining:
            await query.edit_message_reply_markup(None)
            await context.bot.send_message(
                chat_id,
                f"🚫 <b>{ticker}</b> blocked — ${notional:.0f} exceeds remaining budget ${remaining:.0f}",
                parse_mode="HTML",
            )
            return

        await query.edit_message_reply_markup(None)

        # Check market hours before placing — fractional orders need to fill immediately for OCO
        b = Broker(os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY"))
        market_open = await asyncio.get_event_loop().run_in_executor(None, b.is_market_open)
        if not market_open:
            await context.bot.send_message(
                chat_id,
                f"🔴 <b>Market closed</b> — come back during market hours (9:30am–4pm ET) to execute <b>{ticker}</b>.",
                parse_mode="HTML",
            )
            return

        price = await asyncio.get_event_loop().run_in_executor(None, b.get_current_price, ticker)
        is_fractional = int(notional / price) < 1 if price > 0 else False
        wait_msg = "⏳ Placing fractional order — awaiting fill & OCO setup (up to 60s)..." if is_fractional else "⏳ Placing order..."
        status = await context.bot.send_message(chat_id, wait_msg)

        try:
            order, error = await asyncio.get_event_loop().run_in_executor(
                None, lambda: b.place_order(ticker, notional, trade["take_profit_pct"], trade["stop_loss_pct"])
            )
        except Exception as e:
            await status.edit_text(f"❌ <b>{ticker}</b>: {e}", parse_mode="HTML")
            return

        if order is None:
            await status.edit_text(f"❌ <b>{ticker}</b>: {error}", parse_mode="HTML")
            return

        record_spend(notional)
        new_remaining = get_remaining()

        shares_str = f"{order['shares']:.4f}" if order["fractional"] else str(order["shares"])
        oco_line = ""
        if order["fractional"]:
            if order["oco_protected"]:
                oco_line = f"\n🛡 OCO set — TP/SL auto-managed  <code>{order['oco_id']}</code>"
            else:
                oco_line = (
                    f"\n⚠️ OCO failed — set TP ${order['take_profit_price']:.2f} / SL ${order['stop_loss_price']:.2f} manually"
                    + (f"\n<code>Error: {error}</code>" if error else "")
                )

        base_msg = (
            f"✅ <b>{ticker}</b>: {shares_str} shares @ ~${order['entry_price']:.2f}\n"
            f"TP ${order['take_profit_price']:.2f}  |  SL ${order['stop_loss_price']:.2f}"
            f"{oco_line}\n"
            f"<code>{order['id']}</code>\n"
            f"💰 ${new_remaining:.0f} remaining today"
        )
        # Show error only for whole-share orders (fractional error shown in oco_line)
        if error and not order["fractional"]:
            base_msg += f"\n⚠️ {error}"

        await status.edit_text(base_msg, parse_mode="HTML")

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
    app.add_handler(CommandHandler("account", cmd_account))
    app.add_handler(CommandHandler("budget", cmd_budget))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
