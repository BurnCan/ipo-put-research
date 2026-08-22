from dataclasses import asdict, dataclass
from datetime import date

from sqlalchemy import select

from app.models import (Company, DailyPrice, IPO, IPOLockup, LockupEventAnalysis,
                        LockupSignalSnapshot, Security)
from .constants import OUTCOME_VERSION, SNAPSHOT_OFFSETS, SNAPSHOT_VERSION
from .lockup_outcomes import compute_event_outcome
from .lockup_snapshots import compute_snapshot, get_price_history_as_of
from .sessions import (align_event_trade_date, event_date_with_source,
                       get_trading_session_offset, projected_weekday_offset)


@dataclass
class AnalysisReport:
    ipos_seen: int = 0
    lockups_seen: int = 0
    events_aligned: int = 0
    snapshots_created: int = 0
    snapshots_updated: int = 0
    outcomes_created: int = 0
    outcomes_updated: int = 0
    upcoming: int = 0
    incomplete: int = 0
    complete: int = 0
    no_market_history: int = 0
    errors: int = 0

    def to_dict(self): return asdict(self)


def _assign(row, values):
    columns = {column.name for column in row.__table__.columns} - {"id", "created_at", "updated_at"}
    for name, value in values.items():
        if name in columns:
            setattr(row, name, value)


def recompute_lockup_analysis(db, lockup: IPOLockup, security: Security | None = None,
                              *, report: AnalysisReport | None = None):
    report = report or AnalysisReport()
    ipo = db.get(IPO, lockup.ipo_id)
    event_date, source = event_date_with_source(lockup)
    if ipo is None or event_date is None:
        report.errors += 1
        return report
    security = security or db.scalar(select(Security).where(
        Security.company_id == ipo.company_id, Security.is_primary.is_(True)).order_by(Security.id))
    if security is None:
        report.no_market_history += 1
        return report
    bars = list(db.scalars(select(DailyPrice).where(DailyPrice.security_id == security.id)
                           .order_by(DailyPrice.trade_date)))
    if not bars:
        report.no_market_history += 1
        return report
    event_trade_date = align_event_trade_date(bars, event_date)
    report.events_aligned += int(event_trade_date is not None)
    by_date = {bar.trade_date: bar for bar in bars}
    for offset in SNAPSHOT_OFFSETS:
        if event_trade_date is not None:
            observation = get_trading_session_offset(bars, event_trade_date, offset)
        else:
            # Prospective fallback: weekdays identify eligible observation dates;
            # only an actual stored bar can become a snapshot.
            observation = by_date.get(projected_weekday_offset(event_date, offset))
        if observation is None or observation.trade_date > bars[-1].trade_date:
            continue
        as_of = get_price_history_as_of(db, security.id, observation.trade_date)
        values = compute_snapshot(as_of, ipo, lockup, observation_offset=offset,
                                  event_date=event_date, event_date_source=source,
                                  event_trade_date=event_trade_date)
        values.update(ipo_id=ipo.id, lockup_id=lockup.id, security_id=security.id,
                      snapshot_version=SNAPSHOT_VERSION)
        row = db.scalar(select(LockupSignalSnapshot).where(
            LockupSignalSnapshot.lockup_id == lockup.id,
            LockupSignalSnapshot.security_id == security.id,
            LockupSignalSnapshot.observation_offset == offset,
            LockupSignalSnapshot.snapshot_version == SNAPSHOT_VERSION))
        if row is None:
            row = LockupSignalSnapshot(); db.add(row); report.snapshots_created += 1
        else:
            report.snapshots_updated += 1
        _assign(row, values)
    outcome_values = compute_event_outcome(bars, event_date, event_trade_date)
    outcome_values.update(ipo_id=ipo.id, lockup_id=lockup.id, security_id=security.id,
                          event_date_source=source, outcome_version=OUTCOME_VERSION)
    outcome = db.scalar(select(LockupEventAnalysis).where(
        LockupEventAnalysis.lockup_id == lockup.id,
        LockupEventAnalysis.security_id == security.id,
        LockupEventAnalysis.outcome_version == OUTCOME_VERSION))
    if outcome is None:
        outcome = LockupEventAnalysis(); db.add(outcome); report.outcomes_created += 1
    else:
        report.outcomes_updated += 1
    _assign(outcome, outcome_values)
    status = outcome_values["event_status"]
    if status == "upcoming": report.upcoming += 1
    elif status == "complete": report.complete += 1
    else: report.incomplete += 1
    db.commit()
    return report


def recompute_lockup_analyses(db, *, limit=None, ipo_id=None, lockup_id=None, ticker=None, recompute=False):
    """Analyze stored data only. ``recompute`` is accepted for CLI/API symmetry;
    versioned rows are deterministically refreshed on every run regardless."""
    report = AnalysisReport()
    stmt = select(IPOLockup, IPO).join(IPO, IPO.id == IPOLockup.ipo_id).join(Company, Company.id == IPO.company_id)
    if lockup_id is not None:
        stmt = stmt.where(IPOLockup.id == lockup_id)
    else:
        stmt = stmt.where(IPOLockup.id == IPO.primary_lockup_id)
    if ipo_id is not None: stmt = stmt.where(IPO.id == ipo_id)
    if ticker: stmt = stmt.where(Company.ticker.ilike(ticker.strip()))
    if limit is not None: stmt = stmt.limit(limit)
    rows = db.execute(stmt.order_by(IPO.id)).all()
    report.ipos_seen = len({ipo.id for _, ipo in rows})
    report.lockups_seen = len(rows)
    for lockup, _ in rows:
        try:
            recompute_lockup_analysis(db, lockup, report=report)
        except Exception:
            db.rollback(); report.errors += 1
    return report
