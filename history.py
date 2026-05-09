"""
Rolling 30-day context window for ticker mentions.
Loads/saves to kevin_rolling.json.
"""

import json
from datetime import date, datetime, timedelta
from pathlib import Path

HISTORY_FILE = Path("rolling_history.json")
WINDOW_DAYS = 30


def load_history() -> dict:
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text())
    return {"entries": []}


def save_history(history: dict):
    HISTORY_FILE.write_text(json.dumps(history, indent=2))


def add_today(history: dict, trades: list, skipped: list, market_sentiment: str):
    """Append today's analysis to history and prune old entries."""
    today = date.today().isoformat()

    # Remove existing entry for today if re-analyzing
    history["entries"] = [e for e in history["entries"] if e["date"] != today]

    entry = {
        "date": today,
        "market_sentiment": market_sentiment,
        "tickers": [],
    }

    for trade in trades:
        entry["tickers"].append({
            "ticker": trade["ticker"],
            "sentiment": "bullish",
            "conviction": trade["conviction"],
            "green_flags": trade.get("green_flags", 0),
            "red_flags": trade.get("red_flags", 0),
        })

    for skip in skipped:
        entry["tickers"].append({
            "ticker": skip.get("ticker", ""),
            "sentiment": "bearish",
            "conviction": "none",
            "green_flags": 0,
            "red_flags": 0,
        })

    history["entries"].append(entry)

    # Prune entries older than WINDOW_DAYS
    cutoff = (date.today() - timedelta(days=WINDOW_DAYS)).isoformat()
    history["entries"] = [e for e in history["entries"] if e["date"] >= cutoff]

    return history


def build_context_summary(history: dict) -> str:
    """
    Build a compact summary of past N days for injection into Claude's prompt.
    Returns empty string if no history.
    """
    entries = history.get("entries", [])
    if not entries:
        return ""

    # Aggregate per ticker
    ticker_data = {}
    for entry in entries:
        d = entry["date"]
        for t in entry["tickers"]:
            sym = t["ticker"].upper()
            if not sym or len(sym) > 6:
                continue
            if sym not in ticker_data:
                ticker_data[sym] = {
                    "bullish_days": [],
                    "bearish_days": [],
                    "convictions": [],
                    "last_seen": d,
                }
            td = ticker_data[sym]
            if t["sentiment"] == "bullish":
                td["bullish_days"].append(d)
                td["convictions"].append(t["conviction"])
            else:
                td["bearish_days"].append(d)
            if d > td["last_seen"]:
                td["last_seen"] = d

    if not ticker_data:
        return ""

    lines = ["HISTORICAL CONTEXT (past 30 days of analyst data):"]
    lines.append("Use this to adjust conviction — repeated bullish mentions = stronger signal, first-time mentions = weaker signal.\n")

    # Sort by total mentions descending
    sorted_tickers = sorted(
        ticker_data.items(),
        key=lambda x: len(x[1]["bullish_days"]) + len(x[1]["bearish_days"]),
        reverse=True,
    )

    for sym, data in sorted_tickers:
        bull = len(data["bullish_days"])
        bear = len(data["bearish_days"])
        total = bull + bear
        if total == 0:
            continue

        last = data["last_seen"]
        days_ago = (date.today() - date.fromisoformat(last)).days

        # Determine trend
        if bull > 0 and bear == 0:
            trend = f"consistently bullish ({bull}d)"
        elif bear > 0 and bull == 0:
            trend = f"consistently bearish ({bear}d)"
        elif bull > bear:
            trend = f"mostly bullish ({bull}b/{bear}bear)"
        elif bear > bull:
            trend = f"mostly bearish ({bear}b/{bull}bull)"
        else:
            trend = f"mixed ({bull}b/{bear}bear)"

        last_str = "today" if days_ago == 0 else f"{days_ago}d ago"
        lines.append(f"  {sym}: {trend}, last seen {last_str}")

    return "\n".join(lines)


def get_ticker_history(history: dict, ticker: str) -> dict:
    """Get summary for a specific ticker — used for trade card display."""
    sym = ticker.upper()
    bull_days = []
    bear_days = []

    for entry in history.get("entries", []):
        for t in entry["tickers"]:
            if t["ticker"].upper() == sym:
                if t["sentiment"] == "bullish":
                    bull_days.append(entry["date"])
                else:
                    bear_days.append(entry["date"])

    return {
        "ticker": sym,
        "bullish_days": len(bull_days),
        "bearish_days": len(bear_days),
        "total_days": len(bull_days) + len(bear_days),
        "first_mention": min(bull_days + bear_days) if (bull_days or bear_days) else None,
    }


def seed_from_kevin_history(kevin_history_path: str = "kevin_history.json"):
    """
    One-time: seed rolling history from the 30-day batch analysis.
    Only call this once to bootstrap.
    """
    from pathlib import Path
    kh_path = Path(kevin_history_path)
    if not kh_path.exists():
        print("kevin_history.json not found")
        return

    kh = json.loads(kh_path.read_text())
    history = load_history()

    # We don't have per-day breakdown from batch analysis,
    # so create synthetic entries spread across last 30 days
    # based on mention counts (approximate)
    today = date.today()
    tickers = kh.get("tickers", [])

    # Use mention_count as proxy for how many days mentioned
    for ticker_data in tickers:
        sym = ticker_data["ticker"]
        bull = ticker_data["bullish"]
        bear = ticker_data["bearish"]
        total = ticker_data["mention_count"]

        # Spread mentions across last 30 days
        for i in range(min(total, 30)):
            day = (today - timedelta(days=i)).isoformat()

            # Find or create entry for this day
            entry = next((e for e in history["entries"] if e["date"] == day), None)
            if not entry:
                entry = {"date": day, "market_sentiment": "neutral", "tickers": []}
                history["entries"].append(entry)

            sentiment = "bullish" if bull >= bear else "bearish"
            entry["tickers"].append({
                "ticker": sym,
                "sentiment": sentiment,
                "conviction": "medium",
                "green_flags": 0,
                "red_flags": 0,
            })

    # Prune duplicates and sort
    history["entries"] = sorted(history["entries"], key=lambda e: e["date"])
    save_history(history)
    print(f"Seeded rolling history with {len(tickers)} tickers across last 30 days")
    print(f"Saved to {HISTORY_FILE}")


if __name__ == "__main__":
    seed_from_kevin_history()
