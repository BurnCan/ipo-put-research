import math
import statistics
from datetime import date

from sqlalchemy import select

from app.models import DailyPrice


def get_price_history_as_of(db, security_id: int, cutoff_date: date):
    """The required cutoff is the code-level look-ahead guardrail."""
    return list(db.scalars(select(DailyPrice).where(
        DailyPrice.security_id == security_id, DailyPrice.trade_date <= cutoff_date
    ).order_by(DailyPrice.trade_date)))


def _ratio(a, b):
    return a / b - 1 if a is not None and b not in (None, 0) else None


def _mean(values):
    return sum(values) / len(values) if values else None


def _exact_window(bars, n):
    return bars[-n:] if len(bars) >= n else None


def _annualized_vol(bars, n):
    # Exactly N close-to-close returns need N+1 bars; sample standard deviation.
    if len(bars) < n + 1:
        return None
    sample = bars[-(n + 1):]
    returns = [float(sample[i].close) / float(sample[i - 1].close) - 1 for i in range(1, len(sample))]
    return statistics.stdev(returns) * math.sqrt(252) if len(returns) >= 2 else None


def compute_snapshot(bars, ipo, lockup, *, observation_offset, event_date,
                     event_date_source, event_trade_date):
    """Compute exclusively from a caller-supplied as-of history."""
    current = bars[-1]
    close = float(current.close)
    high = max(float(b.high) for b in bars)
    low = min(float(b.low) for b in bars)
    result = {
        "observation_offset": observation_offset, "observation_date": current.trade_date,
        "data_cutoff_date": current.trade_date, "event_date": event_date,
        "event_date_source": event_date_source, "event_trade_date": event_trade_date,
        "trading_sessions_to_event": abs(observation_offset),
        "lockup_duration_days": lockup.duration_days, "lockup_holder_group": lockup.holder_group,
        "lockup_type": lockup.lockup_type, "lockup_confidence": lockup.confidence,
        "ipo_price": ipo.ipo_price, "primary_shares": ipo.primary_shares,
        "secondary_shares": ipo.secondary_shares, "shares_offered": ipo.shares_offered,
        "deal_size": ipo.deal_size, "days_since_ipo": ((current.trade_date - ipo.ipo_date).days
                                                        if ipo.ipo_date else None),
        "trading_sessions_since_first_trade": len(bars) - 1,
        "available_history_sessions": len(bars), "close": close,
        "post_ipo_high_to_date": high, "post_ipo_low_to_date": low,
        "return_from_ipo_price": _ratio(close, float(ipo.ipo_price)) if ipo.ipo_price else None,
        "drawdown_from_post_ipo_high": _ratio(close, high),
        "position_in_post_ipo_range": (close - low) / (high - low) if high != low else None,
        "ipo_gain_retention": ((close - float(ipo.ipo_price)) / (high - float(ipo.ipo_price))
                               if ipo.ipo_price is not None and high > float(ipo.ipo_price) else None),
        "secondary_share_fraction": (float(ipo.secondary_shares) / float(ipo.shares_offered)
                                     if ipo.secondary_shares is not None and ipo.shares_offered not in (None, 0) else None),
    }
    for n in (5, 10, 20, 40):
        result[f"return_{n}d"] = _ratio(close, float(bars[-n - 1].close)) if len(bars) >= n + 1 else None
        window = _exact_window(bars, n)
        result[f"avg_volume_{n}d"] = _mean([b.volume for b in window]) if window else None
        result[f"realized_vol_{n}d"] = _annualized_vol(bars, n)
    for n in (5, 20):
        window = _exact_window(bars, n)
        result[f"avg_dollar_volume_{n}d"] = _mean([float(b.close) * b.volume for b in window]) if window else None
    av5, av20 = result["avg_volume_5d"], result["avg_volume_20d"]
    result["volume_ratio_5d_to_20d"] = av5 / av20 if av5 is not None and av20 else None
    if len(bars) >= 21:
        sample = bars[-21:]
        up, down, ranges = [], [], []
        for previous, bar in zip(sample, sample[1:]):
            previous_close = float(previous.close)
            if float(bar.close) > previous_close: up.append(bar.volume)
            elif float(bar.close) < previous_close: down.append(bar.volume)
            ranges.append((float(bar.high) - float(bar.low)) / previous_close if previous_close else None)
        result["avg_up_day_volume_20d"], result["avg_down_day_volume_20d"] = _mean(up), _mean(down)
        result["down_up_volume_ratio_20d"] = (_mean(down) / _mean(up) if up and down and _mean(up) else None)
        result["avg_daily_range_20d"] = _mean([x for x in ranges if x is not None])
    else:
        result.update(avg_up_day_volume_20d=None, avg_down_day_volume_20d=None,
                      down_up_volume_ratio_20d=None, avg_daily_range_20d=None)
    return result
