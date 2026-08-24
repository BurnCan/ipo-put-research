"""Canonical XNYS coverage and narrowly targeted market-history repair."""
from dataclasses import asdict, dataclass
from datetime import date
from importlib.metadata import version

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Company, DailyPrice, IPOLockup, Security, utc_now
from app.services.event_analysis.constants import SNAPSHOT_OFFSETS
from app.services.event_analysis.sessions import event_date_with_source
from app.services.market_calendar import (
    CALENDAR_ID, CALENDAR_PROVIDER, is_session, resolve_event_session,
    session_offset, sessions_in_range,
)
from app.services.market_data.base import MarketDataProvider


@dataclass(frozen=True)
class CoverageResult:
    security_id: int
    ticker: str
    company_name: str
    requested_start_date: date
    requested_end_date: date
    canonical_start_session: date | None
    canonical_end_session: date | None
    expected_sessions: tuple[date, ...]
    stored_sessions: tuple[date, ...]
    missing_sessions: tuple[date, ...]
    as_of_date: date
    fetchable_missing_sessions: tuple[date, ...]
    future_missing_sessions: tuple[date, ...]
    non_session_stored_dates: tuple[date, ...]
    calendar_id: str = CALENDAR_ID
    calendar_provider: str = CALENDAR_PROVIDER
    calendar_version: str = version(CALENDAR_PROVIDER)

    @property
    def expected_session_count(self): return len(self.expected_sessions)
    @property
    def stored_expected_session_count(self): return len(set(self.expected_sessions) & set(self.stored_sessions))
    @property
    def missing_session_count(self): return len(self.missing_sessions)
    @property
    def missing_sessions_total(self): return len(self.missing_sessions)
    @property
    def coverage_complete(self): return not self.missing_sessions
    @property
    def coverage_ratio(self):
        return self.stored_expected_session_count / self.expected_session_count if self.expected_session_count else 1.0

    def to_dict(self):
        value = asdict(self)
        value.update(expected_session_count=self.expected_session_count,
                     stored_expected_session_count=self.stored_expected_session_count,
                     missing_session_count=self.missing_session_count,
                     missing_sessions_total=self.missing_sessions_total,
                     coverage_complete=self.coverage_complete, coverage_ratio=self.coverage_ratio)
        return value


@dataclass(frozen=True)
class LockupCoveragePlan:
    lockup_id: int
    event_date: date
    event_session: date
    earliest_required_snapshot_session: date
    earliest_feature_session: date
    coverage_start: date
    coverage_end: date
    snapshot_offsets: tuple[int, ...] = SNAPSHOT_OFFSETS
    feature_session_count: int = 21


@dataclass(frozen=True)
class FeatureWindowCoverage:
    observation_session: date
    required_sessions: tuple[date, ...]
    present_sessions: tuple[date, ...]
    missing_sessions: tuple[date, ...]
    complete: bool


@dataclass
class BackfillResult:
    status: str
    coverage_before: CoverageResult
    coverage_after: CoverageResult
    request_ranges: tuple[tuple[date, date], ...]
    provider_requests: int = 0
    bars_fetched: int = 0
    bars_created: int = 0
    bars_updated: int = 0
    provider_no_data: int = 0
    provider_errors: int = 0

    def to_dict(self):
        value = asdict(self)
        value["coverage_before"] = self.coverage_before.to_dict()
        value["coverage_after"] = self.coverage_after.to_dict()
        return value


def coverage(db: Session, security: Security, start_date: date, end_date: date,
             *, provider: str | None = None, as_of_date: date | None = None) -> CoverageResult:
    expected = sessions_in_range(start_date, end_date)
    stmt = select(DailyPrice.trade_date).where(
        DailyPrice.security_id == security.id, DailyPrice.trade_date >= start_date,
        DailyPrice.trade_date <= end_date)
    if provider is not None:
        stmt = stmt.where(DailyPrice.provider == provider)
    stored = tuple(sorted(set(db.scalars(stmt))))
    wanted = set(expected)
    company_name = security.company.name if security.company else db.scalar(
        select(Company.name).where(Company.id == security.company_id))
    missing = tuple(day for day in expected if day not in set(stored))
    cutoff = as_of_date or date.today()
    return CoverageResult(
        security.id, security.ticker, company_name, start_date, end_date,
        expected[0] if expected else None, expected[-1] if expected else None,
        expected, stored, missing, cutoff,
        tuple(day for day in missing if day <= cutoff),
        tuple(day for day in missing if day > cutoff),
        tuple(day for day in stored if day not in wanted and not is_session(day)),
    )


def plan_lockup_coverage(lockup: IPOLockup) -> LockupCoveragePlan:
    event_date, _ = event_date_with_source(lockup)
    if event_date is None:
        raise ValueError("lockup has no known event date")
    event_session = resolve_event_session(event_date).event_session
    earliest_snapshot = session_offset(event_session, min(SNAPSHOT_OFFSETS))
    earliest_feature = session_offset(earliest_snapshot, -20)
    return LockupCoveragePlan(lockup.id, event_date, event_session, earliest_snapshot,
                              earliest_feature, earliest_feature, event_session)


def feature_window_coverage(db: Session, security: Security, observation_session: date,
                            *, provider: str | None = None) -> FeatureWindowCoverage:
    required = tuple(session_offset(observation_session, offset) for offset in range(-20, 1))
    result = coverage(db, security, required[0], required[-1], provider=provider)
    present = tuple(day for day in required if day in set(result.stored_sessions))
    missing = tuple(day for day in required if day not in set(present))
    return FeatureWindowCoverage(observation_session, required, present, missing, not missing)


def provider_request_ranges(missing: tuple[date, ...]) -> tuple[tuple[date, date], ...]:
    """Batch adjacent missing *canonical sessions*, including intervening closures."""
    if not missing:
        return ()
    groups, start, previous = [], missing[0], missing[0]
    for current in missing[1:]:
        if session_offset(previous, 1) != current:
            groups.append((start, previous)); start = current
        previous = current
    groups.append((start, previous))
    return tuple(groups)


def backfill_missing_sessions(db: Session, provider: MarketDataProvider, security: Security,
                              start_date: date, end_date: date, *, as_of_date: date | None = None,
                              dry_run: bool = False) -> BackfillResult:
    cutoff = as_of_date or date.today()
    # Canonical completeness is provider-independent. Provider identity is
    # provenance only and must not trigger duplicate work after a switch.
    before = coverage(db, security, start_date, end_date, as_of_date=cutoff)
    fetchable = before.fetchable_missing_sessions
    ranges = provider_request_ranges(fetchable)
    if not ranges:
        status = "complete" if before.coverage_complete else "future_sessions_only"
        return BackfillResult(status, before, before, ranges)
    symbol = security.provider_symbol or security.ticker
    if not symbol:
        return BackfillResult("no_security_symbol", before, before, ranges)
    if dry_run:
        return BackfillResult("missing_sessions", before, before, ranges)
    result = BackfillResult("missing_sessions", before, before, ranges)
    for range_start, range_end in ranges:
        result.provider_requests += 1
        try:
            bars = provider.get_daily_history(symbol, range_start, range_end)
        except Exception:
            result.provider_errors += 1
            continue
        result.bars_fetched += len(bars)
        if not bars:
            result.provider_no_data += 1
        for bar in bars:
            if bar.trade_date not in set(fetchable) or not is_session(bar.trade_date):
                continue
            row = db.scalar(select(DailyPrice).where(
                DailyPrice.security_id == security.id, DailyPrice.trade_date == bar.trade_date,
                DailyPrice.provider == provider.name))
            values = dict(open=bar.open, high=bar.high, low=bar.low, close=bar.close,
                          volume=bar.volume, adjusted_close=bar.adjusted_close,
                          provider_symbol=symbol, fetched_at=utc_now())
            if row is None:
                db.add(DailyPrice(security_id=security.id, trade_date=bar.trade_date,
                                  provider=provider.name, **values)); result.bars_created += 1
            else:
                for key, value in values.items(): setattr(row, key, value)
                result.bars_updated += 1
        db.flush()
    db.commit()
    result.coverage_after = coverage(db, security, start_date, end_date, as_of_date=cutoff)
    if result.coverage_after.coverage_complete:
        result.status = "complete"
    elif (not result.coverage_after.fetchable_missing_sessions
          and result.coverage_after.future_missing_sessions):
        result.status = "future_sessions_only"
    elif result.provider_errors:
        result.status = "provider_error"
    elif result.provider_no_data and not result.bars_created:
        result.status = "provider_no_data"
    elif result.bars_created or result.bars_updated:
        result.status = "partially_backfilled"
    return result
