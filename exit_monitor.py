"""
Daily exit monitor: runs at 9:35am ET after market open.
Day 7+: close position if P&L < DAY7_MIN_PCT, else hold.
Day 10+: hard exit regardless.
"""

import os
from datetime import date, timedelta

from broker import Broker
from position_tracker import get_all_positions, remove_position

DAY7_MIN_PCT  = 3.0   # min % gain to keep holding at day 7
HARD_EXIT_DAY = 10    # force close regardless of P&L


def _trading_days_since(entry: date) -> int:
    count = 0
    d = entry
    today = date.today()
    while d < today:
        d += timedelta(days=1)
        if d.weekday() < 5:
            count += 1
    return count


async def run_exit_checks(bot, chat_id: int):
    positions = get_all_positions()
    if not positions:
        return

    b = Broker(os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY"))

    if not b.is_market_open():
        return

    # Get live Alpaca positions to verify still open
    try:
        alpaca_open = b.get_open_positions()
    except Exception:
        alpaca_open = set()

    for ticker, pos in list(positions.items()):
        entry_date = date.fromisoformat(pos["entry_date"])
        age = _trading_days_since(entry_date)

        if age < 7:
            continue

        # Position already closed by TP/SL — clean up tracker
        if ticker not in alpaca_open:
            remove_position(ticker)
            continue

        try:
            current_price = b.get_current_price(ticker)
            if current_price <= 0:
                continue
        except Exception:
            continue

        entry_price = pos["entry_price"]
        pnl_pct = round((current_price - entry_price) / entry_price * 100, 2)

        if age >= HARD_EXIT_DAY:
            _close(b, ticker, current_price, pnl_pct, age, reason="hard exit day 10")
            await bot.send_message(
                chat_id,
                f"🚨 <b>{ticker}</b> hard exit — day {age}\n"
                f"Entry ${entry_price:.2f} → ${current_price:.2f}  {pnl_pct:+.1f}%",
                parse_mode="HTML",
            )
        elif pnl_pct < DAY7_MIN_PCT:
            _close(b, ticker, current_price, pnl_pct, age, reason=f"day 7 below +{DAY7_MIN_PCT}%")
            await bot.send_message(
                chat_id,
                f"🕐 <b>{ticker}</b> closed — day {age}, only {pnl_pct:+.1f}% (threshold +{DAY7_MIN_PCT}%)\n"
                f"Entry ${entry_price:.2f} → ${current_price:.2f}",
                parse_mode="HTML",
            )
        else:
            await bot.send_message(
                chat_id,
                f"🕐 <b>{ticker}</b> day {age} check: {pnl_pct:+.1f}% — holding to day {HARD_EXIT_DAY}",
                parse_mode="HTML",
            )


def _close(b: Broker, ticker: str, current_price: float, pnl_pct: float,
           age: int, reason: str):
    try:
        b.close_position(ticker)
        remove_position(ticker)
        print(f"Closed {ticker} at ${current_price:.2f} ({pnl_pct:+.1f}%) — {reason}")
    except Exception as e:
        print(f"Failed to close {ticker}: {e}")
