from datetime import date
import yfinance as yf


def days_until_earnings(ticker: str) -> int | None:
    """Return days until next earnings, or None if unavailable."""
    try:
        cal = yf.Ticker(ticker).calendar
        dates = cal.get("Earnings Date", [])
        if not dates:
            return None
        today = date.today()
        future = [d for d in dates if d >= today]
        if not future:
            return None
        return (min(future) - today).days
    except Exception:
        return None


def earnings_warning(ticker: str, threshold_days: int = 5) -> str | None:
    """Return warning string if earnings within threshold, else None."""
    days = days_until_earnings(ticker)
    if days is None:
        return None
    if days == 0:
        return f"🚨 {ticker} reports earnings TODAY"
    if days <= threshold_days:
        return f"⚠️ {ticker} reports earnings in {days}d — high volatility risk"
    return None
