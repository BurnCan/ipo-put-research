from datetime import date

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Company, DailyPrice, MarketDataBackfillAttempt, Security
from app.services.market_data.coverage import (
    backfill_missing_sessions, known_no_data_sessions,
)
from app.services.market_data.diagnostics import diagnose_market_data_window
from scripts.record_market_data_backfill_attempt import reconcile_attempt


class ProviderThatMustNotRun:
    name = "fake"

    def __init__(self):
        self.calls = []

    def get_daily_history(self, *args):
        self.calls.append(args)
        raise AssertionError("provider called by provenance reconciliation")


@pytest.fixture
def fixture():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = Session(engine)
    company = Company(cik="0000000099", name="Legacy Co", ticker="LEGC")
    security = Security(company=company, ticker="LEGC", provider_symbol="LEGC")
    db.add(security)
    db.commit()
    yield db, security
    db.close()


def record(db, **overrides):
    values = dict(ticker="LEGC", provider="fake", start_date=date(2025, 1, 6),
                  end_date=date(2025, 1, 8), status="no_data")
    values.update(overrides)
    return reconcile_attempt(db, **values)


def test_no_data_reconciliation_is_idempotent_and_never_creates_prices(fixture):
    db, security = fixture
    first = record(db, bars_returned=0, bars_created=0, bars_updated=0)
    row = db.scalar(select(MarketDataBackfillAttempt))
    attempted_at = row.attempted_at
    assert first["status"] == "created"
    assert (row.status, row.bars_returned, row.bars_created, row.bars_updated) == (
        "no_data", 0, 0, 0)
    second = record(db, error_message="ignored on equivalent entry")
    assert second == {**first, "status": "already_present"}
    assert db.query(MarketDataBackfillAttempt).count() == 1
    assert row.attempted_at == attempted_at
    assert db.query(DailyPrice).count() == 0
    assert known_no_data_sessions(
        db, security.id, "fake", (date(2025, 1, 6), date(2025, 1, 7),
                                  date(2025, 1, 8))) == (
        date(2025, 1, 6), date(2025, 1, 7), date(2025, 1, 8))

    provider = ProviderThatMustNotRun()
    plan = backfill_missing_sessions(
        db, provider, security, date(2025, 1, 6), date(2025, 1, 8),
        as_of_date=date(2025, 1, 8), dry_run=True)
    assert plan.status == "known_no_data"
    assert not plan.request_ranges and not provider.calls


def test_error_is_preserved_and_remains_retryable(fixture):
    db, security = fixture
    result = record(db, status="error", error_message="historical timeout")
    row = db.get(MarketDataBackfillAttempt, result["attempt_id"])
    assert row.status == "error" and row.error_message == "historical timeout"
    assert known_no_data_sessions(db, security.id, "fake",
                                  (date(2025, 1, 6),)) == ()
    diagnostic = diagnose_market_data_window(
        db, security.id, "fake",
        (date(2025, 1, 6), date(2025, 1, 7), date(2025, 1, 8)),
        as_of=date(2025, 1, 8))
    assert diagnostic["status"] == "provider_error"
    assert diagnostic["provider_error_count"] == 3
    provider = ProviderThatMustNotRun()
    plan = backfill_missing_sessions(
        db, provider, security, date(2025, 1, 6), date(2025, 1, 8),
        as_of_date=date(2025, 1, 8), dry_run=True)
    assert plan.request_ranges and not provider.calls


def test_dry_run_validates_but_changes_nothing(fixture):
    db, _ = fixture
    result = record(db, dry_run=True)
    assert result["status"] == "would_create" and result["dry_run"] is True
    assert db.query(MarketDataBackfillAttempt).count() == 0
    assert db.query(DailyPrice).count() == 0


@pytest.mark.parametrize("overrides, message", [
    ({"start_date": date(2025, 1, 9), "end_date": date(2025, 1, 8)},
     "start date"),
    ({"ticker": "UNKNOWN"}, "unknown ticker"),
    ({"provider": ""}, "provider identity"),
    ({"status": "invented"}, "status must be"),
])
def test_invalid_reconciliation_persists_nothing(fixture, overrides, message):
    db, _ = fixture
    with pytest.raises(ValueError, match=message):
        record(db, **overrides)
    assert db.query(MarketDataBackfillAttempt).count() == 0


def test_ambiguous_ticker_is_rejected(fixture):
    db, _ = fixture
    other = Company(cik="0000000100", name="Other Legacy Co", ticker="LEGC")
    db.add(Security(company=other, ticker="LEGC", source="manual"))
    db.commit()
    with pytest.raises(ValueError, match="ambiguous"):
        record(db)
    assert db.query(MarketDataBackfillAttempt).count() == 0
