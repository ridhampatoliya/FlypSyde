"""
Rolling 30-day context window for ticker mentions.
Persists to GitHub Gist if GITHUB_TOKEN + GIST_HISTORY_ID are set, else local file.
"""

import json
import os
import urllib.request
import urllib.error
from datetime import date, timedelta
from pathlib import Path

HISTORY_FILE = Path("rolling_history.json")
GIST_FILENAME = "rolling_history.json"
WINDOW_DAYS = 30


# ── Gist helpers ───────────────────────────────────────────────────────────────

def _use_gist() -> bool:
    return bool(os.getenv("GITHUB_TOKEN") and os.getenv("GIST_HISTORY_ID"))


def _gist_headers() -> dict:
    return {
        "Authorization": f"token {os.getenv('GITHUB_TOKEN')}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json",
        "User-Agent": "autoinvest-bot",
    }


def _gist_get() -> dict | None:
    gist_id = os.getenv("GIST_HISTORY_ID")
    req = urllib.request.Request(
        f"https://api.github.com/gists/{gist_id}",
        headers=_gist_headers(),
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        content = data["files"].get(GIST_FILENAME, {}).get("content", "")
        return json.loads(content) if content else None
    except Exception as e:
        print(f"Gist load failed: {e}")
        return None


def _gist_patch(history: dict):
    gist_id = os.getenv("GIST_HISTORY_ID")
    payload = json.dumps({
        "files": {GIST_FILENAME: {"content": json.dumps(history, indent=2)}}
    }).encode()
    req = urllib.request.Request(
        f"https://api.github.com/gists/{gist_id}",
        data=payload,
        headers=_gist_headers(),
        method="PATCH",
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"Gist save failed: {e}")


def create_gist() -> str:
    """One-time: create a new private gist and return its ID."""
    payload = json.dumps({
        "description": "AutoInvest rolling history",
        "public": False,
        "files": {GIST_FILENAME: {"content": json.dumps({"entries": []}, indent=2)}},
    }).encode()
    req = urllib.request.Request(
        "https://api.github.com/gists",
        data=payload,
        headers=_gist_headers(),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
    gist_id = data["id"]
    print(f"Gist created: {gist_id}")
    print(f"Add to Railway env: GIST_HISTORY_ID={gist_id}")
    return gist_id


# ── Public API ─────────────────────────────────────────────────────────────────

def load_history() -> dict:
    if _use_gist():
        result = _gist_get()
        if result is not None:
            return result
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text())
    return {"entries": []}


def save_history(history: dict):
    if _use_gist():
        _gist_patch(history)
    HISTORY_FILE.write_text(json.dumps(history, indent=2))


def add_today(history: dict, trades: list, skipped: list, market_sentiment: str):
    today = date.today().isoformat()
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

    cutoff = (date.today() - timedelta(days=WINDOW_DAYS)).isoformat()
    history["entries"] = [e for e in history["entries"] if e["date"] >= cutoff]

    return history


def build_context_summary(history: dict) -> str:
    entries = history.get("entries", [])
    if not entries:
        return ""

    ticker_data = {}
    for entry in entries:
        d = entry["date"]
        for t in entry["tickers"]:
            sym = t["ticker"].upper()
            if not sym or len(sym) > 6:
                continue
            if sym not in ticker_data:
                ticker_data[sym] = {"bullish_days": [], "bearish_days": [], "last_seen": d}
            td = ticker_data[sym]
            if t["sentiment"] == "bullish":
                td["bullish_days"].append(d)
            else:
                td["bearish_days"].append(d)
            if d > td["last_seen"]:
                td["last_seen"] = d

    if not ticker_data:
        return ""

    lines = ["HISTORICAL CONTEXT (past 30 days of analyst data):"]
    lines.append("Use this to adjust conviction — repeated bullish mentions = stronger signal, first-time mentions = weaker signal.\n")

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
    sym = ticker.upper()
    bull_days, bear_days = [], []
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


def build_history_report(history: dict) -> str:
    """Compact summary for /history Telegram command."""
    entries = sorted(history.get("entries", []), key=lambda e: e["date"], reverse=True)
    if not entries:
        return "No history stored yet."

    lines = [f"📅 <b>Last {min(5, len(entries))} days</b>"]
    for e in entries[:5]:
        bulls = [t["ticker"] for t in e["tickers"] if t["sentiment"] == "bullish"]
        bears = [t["ticker"] for t in e["tickers"] if t["sentiment"] == "bearish"]
        icon = {"bullish": "🟢", "neutral": "🟡", "bearish": "🔴"}.get(e["market_sentiment"], "🟡")
        bull_str = ", ".join(bulls) if bulls else "—"
        bear_str = ", ".join(bears) if bears else "—"
        lines.append(f"\n{icon} <b>{e['date']}</b>")
        lines.append(f"  Bullish: {bull_str}")
        if bears:
            lines.append(f"  Bearish: {bear_str}")

    # Top 5 tickers by total mentions
    ticker_counts = {}
    for e in entries:
        for t in e["tickers"]:
            sym = t["ticker"].upper()
            if sym:
                ticker_counts[sym] = ticker_counts.get(sym, 0) + 1
    top = sorted(ticker_counts.items(), key=lambda x: x[1], reverse=True)[:5]
    if top:
        lines.append(f"\n🔝 <b>Top 30d mentions:</b>")
        lines.append("  " + "  |  ".join(f"{s} ({c}d)" for s, c in top))

    lines.append(f"\n<i>{len(entries)} total days stored</i>")
    return "\n".join(lines)


def seed_from_batch(batch_path: str = "kevin_history.json"):
    """One-time: seed rolling history from batch analysis JSON."""
    kh_path = Path(batch_path)
    if not kh_path.exists():
        print(f"{batch_path} not found")
        return
    kh = json.loads(kh_path.read_text())
    history = {"entries": []}
    today = date.today()
    for td in kh.get("tickers", []):
        sym = td["ticker"]
        bull = td["bullish"]
        bear = td["bearish"]
        total = td["mention_count"]
        sentiment = "bullish" if bull >= bear else "bearish"
        for i in range(min(total, 30)):
            day = (today - timedelta(days=i)).isoformat()
            entry = next((e for e in history["entries"] if e["date"] == day), None)
            if not entry:
                entry = {"date": day, "market_sentiment": "neutral", "tickers": []}
                history["entries"].append(entry)
            entry["tickers"].append({
                "ticker": sym,
                "sentiment": sentiment,
                "conviction": "medium",
                "green_flags": 0,
                "red_flags": 0,
            })
    history["entries"] = sorted(history["entries"], key=lambda e: e["date"])
    save_history(history)
    print(f"Seeded {len(kh.get('tickers', []))} tickers across last 30 days")


if __name__ == "__main__":
    import sys
    if "--create-gist" in sys.argv:
        create_gist()
    elif "--seed" in sys.argv:
        seed_from_batch()
