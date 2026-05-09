DAILY_LIMIT = 1000.0

# Dollar amount per conviction level
POSITION_SIZES = {
    "high":   200.0,
    "medium": 150.0,
    "low":    100.0,
}

# Stop-loss % by conviction — higher conviction gets more breathing room
STOP_LOSS_PCT = {
    "high":   10.0,
    "medium":  7.0,
    "low":     5.0,
}

# Default take-profit % when no price target is mentioned
TAKE_PROFIT_PCT_DEFAULTS = {
    "high":   20.0,
    "medium": 12.0,
    "low":     8.0,
}

# Hard cap — conservative ceiling regardless of stated targets
TAKE_PROFIT_MAX_PCT = 25.0
