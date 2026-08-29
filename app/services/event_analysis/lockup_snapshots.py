import math
import statistics
from datetime import date

from sqlalchemy import select

from app.models import DailyPrice
from app.services.market_calendar import (SessionResolution, is_session,
                                          session_offset, sessions_in_range)


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


def _canonical_window(bars_by_date, end: date, sessions: int):
    """Return an exact canonical window, never an available-row window."""
    start = session_offset(end, -(sessions - 1))
    expected = sessions_in_range(start, end)
    if len(expected) != sessions or any(day not in bars_by_date for day in expected):
        return None
    return [bars_by_date[day] for day in expected]


def _canonical_vol(window):
    if window is None:
        return None
    returns = [float(window[i].close) / float(window[i - 1].close) - 1
               for i in range(1, len(window))]
    return statistics.stdev(returns) * math.sqrt(252) if len(returns) >= 2 else None


def compute_canonical_snapshot(bars_by_date, ipo, lockup, *, observation_offset,
                               event_date, event_date_source,
                               resolution: SessionResolution, as_of_date: date):
    """Compute M6 v2 from date-keyed bars and exact canonical XNYS windows.

    Calendar sessions define every identity/window. Stored rows only answer
    whether a value is available.
    """
    observation = resolution.observation_session
    full_start = session_offset(observation, -40)
    expected = sessions_in_range(full_start, observation)
    present = sum(day in bars_by_date for day in expected)
    base = {
        "observation_offset": observation_offset, "observation_date": observation,
        "data_cutoff_date": min(observation, as_of_date), "event_date": event_date,
        "event_date_source": event_date_source,
        "event_trade_date": resolution.event_session,
        "trading_sessions_to_event": abs(observation_offset),
        "lockup_duration_days": lockup.duration_days,
        "lockup_holder_group": lockup.holder_group, "lockup_type": lockup.lockup_type,
        "lockup_confidence": lockup.confidence, "ipo_price": ipo.ipo_price,
        "primary_shares": ipo.primary_shares, "secondary_shares": ipo.secondary_shares,
        "shares_offered": ipo.shares_offered, "deal_size": ipo.deal_size,
        "secondary_share_fraction": (float(ipo.secondary_shares) / float(ipo.shares_offered)
                                     if ipo.secondary_shares is not None and
                                     ipo.shares_offered not in (None, 0) else None),
        "calendar_id": resolution.calendar_id, "calendar_provider": resolution.calendar_provider,
        "calendar_version": resolution.calendar_version,
        "expected_history_start_date": full_start,
        "expected_history_end_date": observation,
        "expected_history_sessions": len(expected),
        "available_history_sessions": present,
        "missing_history_sessions": len(expected) - present,
    }
    current = bars_by_date.get(observation)
    if observation > as_of_date:
        reason = "observation_not_reached"
    elif current is None:
        reason = "missing_observation_bar"
    else:
        reason = None
    if reason is not None:
        base.update(snapshot_status="unavailable", unavailable_reason=reason,
                    close=None, trading_sessions_since_first_trade=None,
                    days_since_ipo=((observation - ipo.ipo_date).days if ipo.ipo_date else None))
        for name in ("post_ipo_high_to_date", "post_ipo_low_to_date",
                     "return_from_ipo_price", "drawdown_from_post_ipo_high",
                     "position_in_post_ipo_range", "ipo_gain_retention",
                     "volume_ratio_5d_to_20d", "avg_up_day_volume_20d",
                     "avg_down_day_volume_20d", "down_up_volume_ratio_20d",
                     "avg_daily_range_20d"):
            base[name] = None
        for n in (5, 10, 20, 40):
            base[f"return_{n}d"] = None
            base[f"realized_vol_{n}d"] = None
            base[f"avg_volume_{n}d"] = None
        for n in (5, 20):
            base[f"avg_dollar_volume_{n}d"] = None
        return base

    stored_as_of = [(day, bar) for day, bar in sorted(bars_by_date.items())
                    if day <= observation]
    close = float(current.close)
    first_trade = stored_as_of[0][0]
    first_trade_is_session = is_session(first_trade)
    post_ipo_sessions = (sessions_in_range(first_trade, observation)
                         if first_trade_is_session else [])
    post_ipo_complete = (first_trade_is_session and
                         all(day in bars_by_date for day in post_ipo_sessions))
    high = (max(float(bars_by_date[day].high) for day in post_ipo_sessions)
            if post_ipo_complete else None)
    low = (min(float(bars_by_date[day].low) for day in post_ipo_sessions)
           if post_ipo_complete else None)
    base.update(
        close=close, days_since_ipo=((observation - ipo.ipo_date).days if ipo.ipo_date else None),
        trading_sessions_since_first_trade=(len(post_ipo_sessions) - 1
                                            if first_trade_is_session else None),
        post_ipo_high_to_date=high, post_ipo_low_to_date=low,
        return_from_ipo_price=_ratio(close, float(ipo.ipo_price)) if ipo.ipo_price else None,
        drawdown_from_post_ipo_high=(_ratio(close, high) if post_ipo_complete else None),
        position_in_post_ipo_range=((close - low) / (high - low)
                                    if post_ipo_complete and high != low else None),
        ipo_gain_retention=((close - float(ipo.ipo_price)) / (high - float(ipo.ipo_price))
                            if post_ipo_complete and ipo.ipo_price is not None and
                            high > float(ipo.ipo_price) else None),
    )
    feature_history_complete = post_ipo_complete
    for n in (5, 10, 20, 40):
        close_window = _canonical_window(bars_by_date, observation, n + 1)
        volume_window = _canonical_window(bars_by_date, observation, n)
        feature_history_complete &= close_window is not None and volume_window is not None
        base[f"return_{n}d"] = (_ratio(float(close_window[-1].close),
                                             float(close_window[0].close))
                                  if close_window else None)
        base[f"realized_vol_{n}d"] = _canonical_vol(close_window)
        base[f"avg_volume_{n}d"] = (_mean([bar.volume for bar in volume_window])
                                     if volume_window else None)
    for n in (5, 20):
        window = _canonical_window(bars_by_date, observation, n)
        feature_history_complete &= window is not None
        base[f"avg_dollar_volume_{n}d"] = (_mean([float(bar.close) * bar.volume for bar in window])
                                            if window else None)
    av5, av20 = base["avg_volume_5d"], base["avg_volume_20d"]
    base["volume_ratio_5d_to_20d"] = av5 / av20 if av5 is not None and av20 else None
    sample = _canonical_window(bars_by_date, observation, 21)
    feature_history_complete &= sample is not None
    if sample:
        up, down, ranges = [], [], []
        for previous, bar in zip(sample, sample[1:]):
            previous_close = float(previous.close)
            if float(bar.close) > previous_close: up.append(bar.volume)
            elif float(bar.close) < previous_close: down.append(bar.volume)
            ranges.append((float(bar.high) - float(bar.low)) / previous_close
                          if previous_close else None)
        base.update(
            avg_up_day_volume_20d=_mean(up), avg_down_day_volume_20d=_mean(down),
            down_up_volume_ratio_20d=(_mean(down) / _mean(up)
                                      if up and down and _mean(up) else None),
            avg_daily_range_20d=_mean([value for value in ranges if value is not None]))
    else:
        base.update(avg_up_day_volume_20d=None, avg_down_day_volume_20d=None,
                    down_up_volume_ratio_20d=None, avg_daily_range_20d=None)
    base["snapshot_status"] = "complete" if feature_history_complete else "partial"
    base["unavailable_reason"] = (None if feature_history_complete
                                  else "missing_feature_history")
    return base
