"""Focused tests for read-only canonical/provider provenance diagnostics."""
from datetime import date, datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Company, DailyPrice, MarketDataBackfillAttempt, Security
from app.services.market_calendar import sessions_in_range
from app.services.market_data.diagnostics import diagnose_market_data_window


def _db():
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db = Session(engine)
    company = Company(cik="0000000999", name="Sparse History", ticker="VHCP")
    security = Security(company=company, ticker="VHCP", source="test")
    db.add(security)
    db.flush()
    return db, security.id


def _bar(db, security_id, day, provider="other"):
    db.add(DailyPrice(security_id=security_id, trade_date=day, provider=provider,
                      provider_symbol="VHCP", open=1, high=1, low=1, close=1, volume=1))


def _attempt(db, security_id, start, end, status, *, provider="configured", hour=0):
    db.add(MarketDataBackfillAttempt(
        security_id=security_id, provider=provider, requested_start_date=start,
        requested_end_date=end, status=status,
        attempted_at=datetime(2026, 6, 1, hour, tzinfo=timezone.utc)))
    db.flush()


def test_complete_window_and_bar_wins_across_providers():
    db, security_id = _db()
    required = sessions_in_range(date(2026, 5, 4), date(2026, 5, 8))
    for day in required:
        _bar(db, security_id, day)
    _attempt(db, security_id, required[0], required[-1], "error")
    result = diagnose_market_data_window(db, security_id, "configured", required,
                                         as_of=date(2026, 6, 1))
    assert result["status"] == "complete"
    assert result["present_count"] == len(required)


def test_unattempted_and_future_not_reached_are_distinct():
    db, security_id = _db()
    required = (date(2026, 5, 4), date(2026, 5, 5))
    result = diagnose_market_data_window(db, security_id, "configured", required,
                                         as_of=date(2026, 5, 10))
    assert result["status"] == "unattempted_missing"
    assert result["unattempted_missing_sessions"] == list(required)
    future = diagnose_market_data_window(db, security_id, "configured", required,
                                         as_of=date(2026, 5, 1))
    assert future["status"] == "not_reached"


def test_no_data_range_ignores_non_sessions_and_provider_identity():
    db, security_id = _db()
    required = sessions_in_range(date(2026, 5, 4), date(2026, 5, 11))
    _attempt(db, security_id, date(2026, 5, 4), date(2026, 5, 11), "no_data",
             provider="A")
    result = diagnose_market_data_window(db, security_id, "A", required,
                                         as_of=date(2026, 6, 1))
    assert result["status"] == "known_no_data"
    assert result["known_no_data_sessions"] == list(required)
    assert result["present_count"] == 0
    assert result["missing_sessions"] == list(required)
    assert "return_20d" not in result and "realized_vol_20d" not in result
    assert date(2026, 5, 9) not in result["known_no_data_sessions"]  # Saturday
    other = diagnose_market_data_window(db, security_id, "B", required,
                                        as_of=date(2026, 6, 1))
    assert other["status"] == "unattempted_missing"


def test_mixed_error_no_data_partial_and_unattempted():
    db, security_id = _db()
    required = sessions_in_range(date(2026, 5, 4), date(2026, 5, 8))
    _attempt(db, security_id, required[0], required[0], "no_data")
    _attempt(db, security_id, required[1], required[1], "error")
    _attempt(db, security_id, required[2], required[2], "partial")
    result = diagnose_market_data_window(db, security_id, "configured", required,
                                         as_of=date(2026, 6, 1))
    assert result["status"] == "mixed_attempt_history"
    assert result["known_no_data_sessions"] == [required[0]]
    assert result["provider_error_sessions"] == [required[1]]
    assert result["attempted_missing_sessions"] == [required[2]]
    assert result["unattempted_missing_sessions"] == list(required[3:])


def test_latest_overlapping_attempt_deterministically_wins():
    db, security_id = _db()
    day = date(2026, 5, 4)
    _attempt(db, security_id, day, day, "error", hour=1)
    _attempt(db, security_id, day, day, "no_data", hour=2)
    result = diagnose_market_data_window(db, security_id, "configured", (day,),
                                         as_of=date(2026, 6, 1))
    assert result["status"] == "known_no_data"
    _attempt(db, security_id, day, day, "error", hour=3)
    later = diagnose_market_data_window(db, security_id, "configured", (day,),
                                        as_of=date(2026, 6, 1))
    assert later["status"] == "provider_error"


def test_future_and_provider_exhausted_sessions_remain_separate_and_missing():
    db, security_id = _db()
    past, future = date(2026, 5, 4), date(2026, 5, 6)
    _attempt(db, security_id, past, past, "no_data")

    result = diagnose_market_data_window(
        db, security_id, "configured", (past, future), as_of=date(2026, 5, 5))

    assert result["status"] == "mixed_attempt_history"
    assert result["known_no_data_sessions"] == [past]
    assert result["not_reached_sessions"] == [future]
    assert result["missing_sessions"] == [past, future]
    assert result["present_sessions"] == []
