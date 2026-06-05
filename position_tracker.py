"""
Tracks open positions with entry metadata (price, date, conviction).
Persists to GitHub Gist if GITHUB_TOKEN + GIST_POSITIONS_ID are set, else local file.
Used by exit_monitor to decide when to close stale positions.
"""

import json
import os
import urllib.request
from datetime import date
from pathlib import Path

POSITIONS_FILE = Path("positions.json")
GIST_FILENAME  = "positions.json"


# ── Gist helpers ───────────────────────────────────────────────────────────────

def _use_gist() -> bool:
    return bool(os.getenv("GITHUB_TOKEN") and os.getenv("GIST_POSITIONS_ID"))


def _gist_headers() -> dict:
    return {
        "Authorization": f"token {os.getenv('GITHUB_TOKEN')}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json",
        "User-Agent": "autoinvest-bot",
    }


def _gist_get() -> dict | None:
    gist_id = os.getenv("GIST_POSITIONS_ID")
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
        print(f"Positions gist load failed: {e}")
        return None


def _gist_patch(positions: dict):
    gist_id = os.getenv("GIST_POSITIONS_ID")
    payload = json.dumps({
        "files": {GIST_FILENAME: {"content": json.dumps(positions, indent=2)}}
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
        print(f"Positions gist save failed: {e}")


# ── Public API ─────────────────────────────────────────────────────────────────

def load_positions() -> dict:
    if _use_gist():
        data = _gist_get()
        if data is not None:
            return data
    if POSITIONS_FILE.exists():
        return json.loads(POSITIONS_FILE.read_text())
    return {}


def save_positions(positions: dict):
    POSITIONS_FILE.write_text(json.dumps(positions, indent=2))
    if _use_gist():
        _gist_patch(positions)


def add_position(ticker: str, order_id: str, entry_price: float,
                 conviction: str, notional: float):
    positions = load_positions()
    positions[ticker] = {
        "order_id":    order_id,
        "entry_price": entry_price,
        "entry_date":  date.today().isoformat(),
        "conviction":  conviction,
        "notional":    notional,
    }
    save_positions(positions)


def remove_position(ticker: str):
    positions = load_positions()
    positions.pop(ticker, None)
    save_positions(positions)


def get_all_positions() -> dict:
    return load_positions()
