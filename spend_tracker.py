import json
from datetime import date
from pathlib import Path

SPEND_FILE = Path("daily_spend.json")
DAILY_LIMIT = 1000.0


def _load() -> dict:
    if SPEND_FILE.exists():
        return json.loads(SPEND_FILE.read_text())
    return {}


def get_today_spent() -> float:
    return _load().get(date.today().isoformat(), 0.0)


def get_remaining() -> float:
    return max(0.0, DAILY_LIMIT - get_today_spent())


def record_spend(notional: float):
    data = _load()
    today = date.today().isoformat()
    data[today] = round(data.get(today, 0.0) + notional, 2)
    SPEND_FILE.write_text(json.dumps(data, indent=2))


def reset_today():
    data = _load()
    data[date.today().isoformat()] = 0.0
    SPEND_FILE.write_text(json.dumps(data, indent=2))
