def _return(value, base):
    return value / base - 1 if value is not None and base not in (None, 0) else None


def compute_event_outcome(bars, event_date, event_trade_date):
    """Compute event-centered measurements from exact session indexes."""
    latest = bars[-1].trade_date if bars else None
    base = {"event_date": event_date, "event_trade_date": event_trade_date, "as_of_date": latest}
    if event_trade_date is None:
        return {**base, "event_status": "upcoming", "max_post_event_session_available": None}
    index = next(i for i, bar in enumerate(bars) if bar.trade_date == event_trade_date)
    event = bars[index]
    event_close = float(event.close)
    available = len(bars) - index - 1
    status = "event_today" if available == 0 else ("complete" if available >= 40 else "post_event_incomplete")
    result = {**base, "event_status": status, "max_post_event_session_available": available,
              "event_open": float(event.open), "event_high": float(event.high), "event_low": float(event.low),
              "event_close": event_close, "event_volume": event.volume}
    previous = bars[index - 1] if index else None
    previous_close = float(previous.close) if previous else None
    result.update(previous_close=previous_close,
                  event_gap_return=_return(float(event.open), previous_close),
                  event_intraday_return=_return(event_close, float(event.open)),
                  event_close_return=_return(event_close, previous_close))
    for n in (20, 10, 5, 1):
        result[f"pre_{n}d_return"] = _return(event_close, float(bars[index - n].close)) if index >= n else None
    for n in (1, 5, 10, 20, 40):
        result[f"post_{n}d_return"] = _return(float(bars[index + n].close), event_close) if available >= n else None
    for n in (5, 10, 20, 40):
        if available >= n:
            window = bars[index + 1:index + n + 1]
            result[f"bearish_mfe_{n}d"] = max(0.0, (event_close - min(float(b.low) for b in window)) / event_close)
            result[f"bearish_mae_{n}d"] = max(0.0, (max(float(b.high) for b in window) - event_close) / event_close)
        else:
            result[f"bearish_mfe_{n}d"] = result[f"bearish_mae_{n}d"] = None
    baseline = bars[index - 20:index - 5] if index >= 20 else None  # exact offsets -20 .. -6
    baseline_avg = sum(b.volume for b in baseline) / 15 if baseline and len(baseline) == 15 else None
    result["baseline_avg_volume"] = baseline_avg
    result["event_volume_ratio"] = event.volume / baseline_avg if baseline_avg else None
    for n in (5, 10):
        result[f"post_{n}d_avg_volume_ratio"] = (sum(b.volume for b in bars[index + 1:index + n + 1]) / n / baseline_avg
                                                  if baseline_avg and available >= n else None)
    return result
