"""Canonical XNYS coverage and narrowly targeted market-history repair."""
from dataclasses import asdict, dataclass
from datetime import date
from importlib.metadata import version

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (Company, DailyPrice, IPOLockup, MarketDataBackfillAttempt,
                        Security, utc_now)
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
    planned_request_ranges: tuple[tuple[date, date], ...] = ()
    skipped_known_no_data_ranges: tuple[tuple[date, date], ...] = ()
    known_no_data_sessions: int = 0
    known_no_data_ranges: int = 0
    provider_requests_skipped_known_no_data: int = 0
    attempt_records_created: int = 0
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


def known_no_data_sessions(db: Session, security_id: int, provider: str,
                           sessions: tuple[date, ...]) -> tuple[date, ...]:
    """Return requested canonical sessions covered by provider-specific no-data attempts."""
    if not sessions:
        return ()
    attempts = db.scalars(select(MarketDataBackfillAttempt).where(
        MarketDataBackfillAttempt.security_id == security_id,
        MarketDataBackfillAttempt.provider == provider,
        MarketDataBackfillAttempt.status == "no_data",
        MarketDataBackfillAttempt.requested_end_date >= sessions[0],
        MarketDataBackfillAttempt.requested_start_date <= sessions[-1])).all()
    return tuple(day for day in sessions if any(
        row.requested_start_date <= day <= row.requested_end_date for row in attempts))


def record_backfill_attempt(db: Session, security: Security, provider: str,
                            start_date: date, end_date: date, status: str, *,
                            bars_returned: int | None = None,
                            bars_created: int | None = None,
                            bars_updated: int | None = None,
                            error_message: str | None = None,
                            commit: bool = True) -> MarketDataBackfillAttempt:
    """Explicitly record an observed attempt, including legacy/manual provenance."""
    if status not in {"success", "no_data", "partial", "error"}:
        raise ValueError("status must be success, no_data, partial, or error")
    if start_date > end_date:
        raise ValueError("start date must not be after end date")
    row = MarketDataBackfillAttempt(
        security_id=security.id, provider=provider,
        requested_start_date=start_date, requested_end_date=end_date,
        attempted_at=utc_now(), status=status, bars_returned=bars_returned,
        bars_created=bars_created, bars_updated=bars_updated,
        error_message=error_message)
    db.add(row)
    if commit:
        db.commit()
    else:
        db.flush()
    return row


def backfill_missing_sessions(db: Session, provider: MarketDataProvider, security: Security,
                              start_date: date, end_date: date, *, as_of_date: date | None = None,
                              dry_run: bool = False,
                              retry_known_no_data: bool = False) -> BackfillResult:
    cutoff = as_of_date or date.today()
    # Canonical completeness is provider-independent. Provider identity is
    # provenance only and must not trigger duplicate work after a switch.
    before = coverage(db, security, start_date, end_date, as_of_date=cutoff)
    fetchable = before.fetchable_missing_sessions
    planned_ranges = provider_request_ranges(fetchable)
    known = () if retry_known_no_data else known_no_data_sessions(
        db, security.id, provider.name, fetchable)
    known_set = set(known)
    request_sessions = tuple(day for day in fetchable if day not in known_set)
    ranges = provider_request_ranges(request_sessions)
    skipped_ranges = provider_request_ranges(known)
    common = dict(planned_request_ranges=planned_ranges,
                  skipped_known_no_data_ranges=skipped_ranges,
                  known_no_data_sessions=len(known), known_no_data_ranges=len(skipped_ranges),
                  provider_requests_skipped_known_no_data=len(skipped_ranges))
    if not planned_ranges:
        status = "complete" if before.coverage_complete else "future_sessions_only"
        return BackfillResult(status, before, before, ranges, **common)
    if not ranges:
        return BackfillResult("known_no_data", before, before, ranges, **common)
    symbol = security.provider_symbol or security.ticker
    if not symbol:
        return BackfillResult("no_security_symbol", before, before, ranges, **common)
    if dry_run:
        return BackfillResult("missing_sessions", before, before, ranges, **common)
    result = BackfillResult("missing_sessions", before, before, ranges, **common)
    for range_start, range_end in ranges:
        result.provider_requests += 1
        try:
            bars = provider.get_daily_history(symbol, range_start, range_end)
        except Exception as exc:
            result.provider_errors += 1
            record_backfill_attempt(db, security, provider.name, range_start, range_end,
                                    "error", bars_returned=0, bars_created=0,
                                    bars_updated=0, error_message=str(exc), commit=False)
            result.attempt_records_created += 1
            continue
        result.bars_fetched += len(bars)
        if not bars:
            result.provider_no_data += 1
        created_before, updated_before = result.bars_created, result.bars_updated
        requested_days = set(sessions_in_range(range_start, range_end)) & set(request_sessions)
        returned_days = set()
        for bar in bars:
            if bar.trade_date not in set(fetchable) or not is_session(bar.trade_date):
                continue
            returned_days.add(bar.trade_date)
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
        status = ("no_data" if not bars else
                  "success" if requested_days <= returned_days else "partial")
        record_backfill_attempt(
            db, security, provider.name, range_start, range_end, status,
            bars_returned=len(bars), bars_created=result.bars_created - created_before,
            bars_updated=result.bars_updated - updated_before, commit=False)
        result.attempt_records_created += 1
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
