from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Company, DailyPrice, IPO, IPOLockup, Security
from app.services.market_calendar import session_offset, sessions_in_range
from app.services.market_data.base import DailyBar
from app.services.market_data.coverage import (
    backfill_missing_sessions, coverage, feature_window_coverage,
    plan_lockup_coverage,
)


def fixture():
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine); db = Session(engine)
    company = Company(cik='0000000001', name='Sparse Co', ticker='SPRS')
    ipo = IPO(company=company, first_filing_date=date(2025, 1, 1))
    security = Security(company=company, ticker='SPRS', provider_symbol='SPRS', is_primary=True)
    lockup = IPOLockup(ipo=ipo, filing_id=1, holder_group='all', lockup_type='standard',
                      stated_expiration_date=date(2025, 1, 11), confidence=Decimal('1'),
                      parser_name='test', parser_version='1', source_excerpt='x',
                      source_locator='x', evidence_key='coverage-test')
    db.add_all([ipo, security, lockup]); db.commit()
    return db, security, lockup


def add_price(db, security, day, provider='fake'):
    db.add(DailyPrice(security_id=security.id, trade_date=day, provider=provider,
                      provider_symbol=security.ticker, open=10, high=11, low=9,
                      close=10, volume=100)); db.commit()


class Provider:
    name = 'fake'
    def __init__(self, bars=(), error=False): self.bars, self.error, self.calls = bars, error, []
    def get_daily_history(self, symbol, start, end):
        self.calls.append((symbol, start, end))
        if self.error: raise RuntimeError('offline fixture failure')
        return [bar for bar in self.bars if start <= bar.trade_date <= end]


def bar(day):
    return DailyBar(day, Decimal('10'), Decimal('11'), Decimal('9'), Decimal('10'), 100)


def test_range_alignment_missing_and_non_session_reporting():
    db, security, _ = fixture()
    expected = sessions_in_range(date(2025, 1, 4), date(2025, 1, 12))
    assert expected == tuple(date(2025, 1, day) for day in (6, 7, 8, 10))
    for day in expected[1:]: add_price(db, security, day)
    add_price(db, security, date(2025, 1, 11))
    result = coverage(db, security, date(2025, 1, 4), date(2025, 1, 12))
    assert result.canonical_start_session == date(2025, 1, 6)
    assert result.canonical_end_session == date(2025, 1, 10)
    assert result.missing_sessions == (date(2025, 1, 6),)
    assert result.non_session_stored_dates == (date(2025, 1, 11),)


def test_lockup_plan_and_exact_feature_window():
    db, security, lockup = fixture(); plan = plan_lockup_coverage(lockup)
    assert plan.event_session == date(2025, 1, 13)
    assert plan.earliest_required_snapshot_session == session_offset(plan.event_session, -60)
    assert plan.earliest_feature_session == session_offset(plan.earliest_required_snapshot_session, -20)
    observation = date(2025, 1, 10)
    required = tuple(session_offset(observation, n) for n in range(-20, 1))
    for day in required: add_price(db, security, day)
    assert feature_window_coverage(db, security, observation).complete
    db.delete(db.scalar(select(DailyPrice).where(DailyPrice.trade_date == required[5]))); db.commit()
    assert feature_window_coverage(db, security, observation).missing_sessions == (required[5],)


def test_dry_run_future_cap_success_idempotency_and_no_data():
    db, security, _ = fixture(); start, end = date(2025, 1, 6), date(2025, 1, 10)
    wanted = sessions_in_range(start, end); provider = Provider([bar(day) for day in wanted])
    dry = backfill_missing_sessions(db, provider, security, start, end, dry_run=True)
    assert not provider.calls and not db.scalars(select(DailyPrice)).all() and dry.request_ranges
    result = backfill_missing_sessions(db, provider, security, start, end, as_of_date=date(2025, 1, 8))
    assert result.bars_created == 3 and all(call[2] <= date(2025, 1, 8) for call in provider.calls)
    result = backfill_missing_sessions(db, provider, security, start, end)
    assert result.status == 'complete' and result.bars_created == 1
    again = backfill_missing_sessions(db, provider, security, start, end)
    assert again.provider_requests == 0 and again.bars_created == 0
    missing_day = wanted[0]; db.delete(db.scalar(select(DailyPrice).where(DailyPrice.trade_date == missing_day))); db.commit()
    empty = backfill_missing_sessions(db, Provider(), security, start, end)
    assert empty.status == 'provider_no_data' and empty.coverage_after.missing_sessions == (missing_day,)


def test_provider_error_preserves_canonical_gap():
    db, security, _ = fixture()
    result = backfill_missing_sessions(db, Provider(error=True), security,
                                       date(2025, 1, 6), date(2025, 1, 6))
    assert result.status == 'provider_error'
    assert result.coverage_after.missing_sessions == (date(2025, 1, 6),)
