"""Offline formula and session tests for the Milestone 6 analysis layer."""
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from app.services.event_analysis.lockup_outcomes import compute_event_outcome
from app.services.event_analysis.lockup_snapshots import compute_snapshot
from app.services.event_analysis.sessions import (align_event_trade_date, event_date_with_source,
                                                   get_trading_session_offset)


def bar(day, close, *, open_=None, high=None, low=None, volume=100):
    close = float(close)
    return SimpleNamespace(trade_date=day, open=close if open_ is None else open_,
                           high=close if high is None else high, low=close if low is None else low,
                           close=close, volume=volume)


def weekdays(start, count):
    result, day = [], start
    while len(result) < count:
        if day.weekday() < 5: result.append(day)
        day += timedelta(days=1)
    return result


def test_date_provenance_alignment_and_offsets():
    lockup = SimpleNamespace(stated_expiration_date=date(2026, 4, 11),
                             calculated_expiration_date=date(2026, 4, 10))
    assert event_date_with_source(lockup) == (date(2026, 4, 11), "stated")
    lockup.stated_expiration_date = None
    assert event_date_with_source(lockup) == (date(2026, 4, 10), "calculated")
    bars = [bar(date(2026, 4, 10), 10), bar(date(2026, 4, 13), 11), bar(date(2026, 4, 14), 12)]
    assert align_event_trade_date(bars, date(2026, 4, 11)) == date(2026, 4, 13)
    assert align_event_trade_date(bars[:1], date(2026, 4, 11)) is None
    assert get_trading_session_offset(bars, date(2026, 4, 13), -1).trade_date == date(2026, 4, 10)
    assert get_trading_session_offset(bars, date(2026, 4, 13), 1).trade_date == date(2026, 4, 14)


def test_snapshot_exact_windows_and_no_lookahead():
    dates = weekdays(date(2025, 1, 2), 45)
    bars = [bar(day, i + 10, high=i + 11, low=i + 9, volume=100 + i) for i, day in enumerate(dates)]
    ipo = SimpleNamespace(ipo_date=dates[0], ipo_price=10, primary_shares=80, secondary_shares=20,
                          shares_offered=100, deal_size=1000)
    lockup = SimpleNamespace(duration_days=180, holder_group="all", lockup_type="standard", confidence=.9)
    kwargs = dict(observation_offset=-10, event_date=date(2025, 5, 1), event_date_source="stated",
                  event_trade_date=None)
    snapshot = compute_snapshot(bars[:21], ipo, lockup, **kwargs)
    assert snapshot["return_20d"] == pytest.approx(30 / 10 - 1)
    assert snapshot["return_40d"] is None
    assert snapshot["avg_volume_5d"] == pytest.approx(sum(range(116, 121)) / 5)
    assert snapshot["avg_dollar_volume_5d"] == pytest.approx(
        sum((26 + i) * (116 + i) for i in range(5)) / 5)
    assert snapshot["secondary_share_fraction"] == .2
    # Huge future high/low/close cannot alter the caller's already-cut-off history.
    changed_future = bars + [bar(date(2025, 5, 2), 10000, high=20000, low=1)]
    repeated = compute_snapshot(changed_future[:21], ipo, lockup, **kwargs)
    for field in ("close", "return_20d", "post_ipo_high_to_date", "post_ipo_low_to_date",
                  "drawdown_from_post_ipo_high", "position_in_post_ipo_range"):
        assert repeated[field] == snapshot[field]


def test_outcomes_alliance_event_returns_excursions_volume_and_completeness():
    dates = weekdays(date(2026, 3, 3), 61)
    bars = [bar(day, 20 + i / 100, open_=20 + i / 100, high=21 + i / 100,
                low=19 + i / 100, volume=100) for i, day in enumerate(dates)]
    event_index = 25
    bars[event_index] = bar(date(2026, 4, 7), 22.29, open_=22, high=23, low=21, volume=300)
    # Preserve the real Alliance-style +1 close expectation.
    bars[event_index + 1] = bar(date(2026, 4, 8), 23.56, high=24, low=20, volume=200)
    outcome = compute_event_outcome(bars, date(2026, 4, 7), date(2026, 4, 7))
    assert outcome["post_1d_return"] == pytest.approx(23.56 / 22.29 - 1)
    assert outcome["event_gap_return"] == pytest.approx(22 / float(bars[event_index - 1].close) - 1)
    assert outcome["event_intraday_return"] == pytest.approx(22.29 / 22 - 1)
    assert outcome["event_close_return"] == pytest.approx(22.29 / float(bars[event_index - 1].close) - 1)
    assert outcome["baseline_avg_volume"] == 100
    assert outcome["event_volume_ratio"] == 3
    assert outcome["bearish_mfe_5d"] == pytest.approx((22.29 - min(b.low for b in bars[26:31])) / 22.29)
    assert outcome["bearish_mae_5d"] == pytest.approx((max(b.high for b in bars[26:31]) - 22.29) / 22.29)
    assert outcome["event_status"] == "post_event_incomplete"
    assert outcome["max_post_event_session_available"] == 35


def test_upcoming_and_event_today_status_are_explicit():
    bars = [bar(date(2026, 4, 6), 21.63), bar(date(2026, 4, 7), 22.29)]
    upcoming = compute_event_outcome(bars[:1], date(2026, 4, 7), None)
    today = compute_event_outcome(bars, date(2026, 4, 7), date(2026, 4, 7))
    assert upcoming["event_status"] == "upcoming"
    assert upcoming["max_post_event_session_available"] is None
    assert today["event_status"] == "event_today"
    assert today["max_post_event_session_available"] == 0
