from datetime import date


def event_date_with_source(lockup):
    """Prefer the explicitly stated date and retain its provenance."""
    if lockup.stated_expiration_date is not None:
        return lockup.stated_expiration_date, "stated"
    if lockup.calculated_expiration_date is not None:
        return lockup.calculated_expiration_date, "calculated"
    return None, None


def align_event_trade_date(bars, event_date: date):
    """First stored session on/after the calendar event; never a prior bar."""
    return next((bar.trade_date for bar in bars if bar.trade_date >= event_date), None)


def get_trading_session_offset(bars, anchor_date: date, offset: int):
    dates = [bar.trade_date for bar in bars]
    try:
        anchor = dates.index(anchor_date)
    except ValueError:
        return None
    target = anchor + offset
    return bars[target] if 0 <= target < len(bars) else None
