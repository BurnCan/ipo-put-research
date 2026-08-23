"""Read-only parity audit for historical M6 stored-bar session identities."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date
from importlib.metadata import version

from sqlalchemy import select

from app.models import (Company, DailyPrice, IPO, IPOLockup,
                        LockupEventAnalysis, LockupSignalSnapshot)
from app.services.backtest.analysis import FROZEN_HYPOTHESES
from app.services.market_calendar import (CALENDAR_ID, CALENDAR_PROVIDER,
                                          resolve_observation_session,
                                          session_offset)
from .sessions import event_date_with_source
from .constants import OUTCOME_VERSION, SNAPSHOT_VERSION

HYPOTHESIS_ID = "m7_return20_vol20_minus5_post20"


@dataclass(frozen=True)
class SessionParityResult:
    ipo_id: int
    lockup_id: int
    security_id: int
    ticker: str | None
    company_name: str
    snapshot_id: int
    snapshot_version: str
    observation_offset: int
    event_date: date | None
    event_date_source: str | None
    stored_event_trade_date: date | None
    canonical_event_trade_date: date | None
    stored_observation_date: date | None
    canonical_observation_date: date | None
    event_session_match: bool
    observation_session_match: bool
    mismatch_type: str
    expected_sessions: tuple[date, ...]
    stored_sessions_in_expected_window: tuple[date, ...]
    missing_expected_sessions: tuple[date, ...]
    old_bar_offset_reproduced: bool
    return_20d: float | None
    realized_vol_20d: float | None

    @property
    def expected_session_count(self): return len(self.expected_sessions)

    @property
    def stored_expected_session_count(self): return len(self.stored_sessions_in_expected_window)

    @property
    def missing_expected_session_count(self): return len(self.missing_expected_sessions)

    def to_dict(self):
        value = asdict(self)
        value.update(expected_session_count=self.expected_session_count,
                     stored_expected_session_count=self.stored_expected_session_count,
                     missing_expected_session_count=self.missing_expected_session_count)
        return value


def _canonical_window(observation: date, event: date) -> tuple[date, ...]:
    """Walk canonical sessions inclusively; no weekday/calendar approximation."""
    direction = 1 if observation <= event else -1
    result, current = [], observation
    while True:
        result.append(current)
        if current == event: break
        current = session_offset(current, direction)
    return tuple(result)


def _classification(event_match, observation_match, missing, reproduced):
    if event_match and observation_match: return "exact_match"
    if not event_match and not observation_match: return "event_and_observation_mismatch"
    if not event_match: return "event_session_mismatch"
    if missing and reproduced: return "observation_session_mismatch"
    return "observation_session_mismatch"


def _audit_one(db, snapshot, lockup, company):
    event_date, source = event_date_with_source(lockup)
    canonical_event = canonical_observation = None
    expected = ()
    if event_date is not None and snapshot.observation_offset is not None:
        resolution = resolve_observation_session(event_date, snapshot.observation_offset)
        canonical_event, canonical_observation = resolution.event_session, resolution.observation_session
        expected = _canonical_window(canonical_observation, canonical_event)
    bars = list(db.scalars(select(DailyPrice).where(
        DailyPrice.security_id == snapshot.security_id).order_by(DailyPrice.trade_date)))
    dates = [bar.trade_date for bar in bars]
    stored = tuple(day for day in expected if day in set(dates))
    missing = tuple(day for day in expected if day not in set(dates))
    reproduced = False
    if snapshot.event_trade_date in dates:
        target = dates.index(snapshot.event_trade_date) + snapshot.observation_offset
        reproduced = 0 <= target < len(dates) and dates[target] == snapshot.observation_date
    required = (event_date, snapshot.event_trade_date, snapshot.observation_date,
                snapshot.observation_offset)
    event_match = canonical_event is not None and snapshot.event_trade_date == canonical_event
    observation_match = (canonical_observation is not None and
                         snapshot.observation_date == canonical_observation)
    mismatch = ("missing_required_fields" if any(value is None for value in required)
                else _classification(event_match, observation_match, missing, reproduced))
    return SessionParityResult(
        snapshot.ipo_id, snapshot.lockup_id, snapshot.security_id, company.ticker,
        company.name, snapshot.id, snapshot.snapshot_version,
        snapshot.observation_offset, event_date, source, snapshot.event_trade_date,
        canonical_event, snapshot.observation_date, canonical_observation,
        event_match, observation_match, mismatch, expected, stored, missing,
        reproduced, float(snapshot.return_20d) if snapshot.return_20d is not None else None,
        float(snapshot.realized_vol_20d) if snapshot.realized_vol_20d is not None else None)


def audit_m6_session_parity(db, *, classification_status="classified",
                            candidate_type="operating_company_ipo", offering_status="priced",
                            primary_lockup_only=True, ticker=None, ipo_id=None,
                            lockup_id=None, limit=None):
    """Audit snapshots without flushing, committing, or assigning ORM attributes."""
    cohort = (select(IPOLockup.id).join(IPO, IPO.id == IPOLockup.ipo_id)
              .join(Company, Company.id == IPO.company_id))
    if classification_status is not None: cohort = cohort.where(IPO.classification_status == classification_status)
    if candidate_type is not None: cohort = cohort.where(IPO.candidate_type == candidate_type)
    if offering_status is not None: cohort = cohort.where(IPO.offering_status == offering_status)
    if primary_lockup_only: cohort = cohort.where(IPOLockup.id == IPO.primary_lockup_id)
    if ticker: cohort = cohort.where(Company.ticker.ilike(ticker.strip()))
    if ipo_id is not None: cohort = cohort.where(IPO.id == ipo_id)
    if lockup_id is not None: cohort = cohort.where(IPOLockup.id == lockup_id)
    cohort = cohort.order_by(IPOLockup.id)
    if limit is not None: cohort = cohort.limit(limit)
    stmt = (select(LockupSignalSnapshot, IPOLockup, Company)
            .join(IPOLockup, IPOLockup.id == LockupSignalSnapshot.lockup_id)
            .join(IPO, IPO.id == LockupSignalSnapshot.ipo_id)
            .join(Company, Company.id == IPO.company_id)
            .where(LockupSignalSnapshot.lockup_id.in_(cohort)))
    rows = [_audit_one(db, *row) for row in db.execute(stmt)]
    return sorted(rows, key=lambda r: ((r.ticker or ""), r.lockup_id,
                                       r.observation_offset, r.snapshot_id))


def summarize_session_parity(db, rows):
    counts = Counter(row.mismatch_type for row in rows)
    by_offset = defaultdict(Counter)
    for row in rows: by_offset[str(row.observation_offset)][row.mismatch_type] += 1
    # M7 discovery used complete feature/outcome rows at the frozen offset.
    spec = FROZEN_HYPOTHESES[HYPOTHESIS_ID]
    outcome_keys = set(db.execute(select(
        LockupEventAnalysis.lockup_id, LockupEventAnalysis.security_id).where(
        LockupEventAnalysis.outcome_version == OUTCOME_VERSION,
        LockupEventAnalysis.post_20d_return.is_not(None))).all())
    discovery_by_lockup = {}
    for r in rows:
        if (r.snapshot_version == SNAPSHOT_VERSION and
                r.observation_offset == spec.observation_offset and
                 r.return_20d is not None and r.realized_vol_20d is not None and
                (r.lockup_id, r.security_id) in outcome_keys):
            discovery_by_lockup.setdefault(r.lockup_id, r)
    discovery = list(discovery_by_lockup.values())
    affected, recomputable = [], 0
    for row in discovery:
        if row.observation_session_match: continue
        affected.append({"ticker": row.ticker, "lockup_id": row.lockup_id})
        if row.canonical_observation_date is not None:
            dates = list(db.scalars(select(DailyPrice.trade_date).where(
                DailyPrice.security_id == row.security_id,
                DailyPrice.trade_date <= row.canonical_observation_date
            ).order_by(DailyPrice.trade_date)))
            recomputable += int(bool(dates) and dates[-1] == row.canonical_observation_date and len(dates) >= 21)
    mismatch_count = len(affected)
    return {
        "snapshots_seen": len(rows), "exact_matches": counts["exact_match"],
        "event_session_mismatches": counts["event_session_mismatch"],
        "observation_session_mismatches": counts["observation_session_mismatch"],
        "event_and_observation_mismatches": counts["event_and_observation_mismatch"],
        "missing_required_fields": counts["missing_required_fields"],
        "sparse_market_history_cases": sum(r.mismatch_type != "exact_match" and
                                           bool(r.missing_expected_sessions) and
                                           r.old_bar_offset_reproduced for r in rows),
        "unexplained_mismatches": sum(r.mismatch_type != "exact_match" and
                                      not r.old_bar_offset_reproduced for r in rows),
        "canonical_calendar_id": CALENDAR_ID,
        "canonical_calendar_provider": CALENDAR_PROVIDER,
        "canonical_calendar_version": version(CALENDAR_PROVIDER),
        "by_offset": {key: {"snapshots_seen": sum(value.values()), **dict(sorted(value.items()))}
                      for key, value in sorted(by_offset.items(), key=lambda item: int(item[0]))},
        "hypothesis_id": HYPOTHESIS_ID,
        "m7_discovery_events": len(discovery),
        "m7_events_with_session_mismatch": mismatch_count,
        "m7_events_with_exact_session_match": len(discovery) - mismatch_count,
        "m7_affected_events": sorted(affected, key=lambda x: ((x["ticker"] or ""), x["lockup_id"])),
        "canonical_features_recomputable": recomputable,
        "canonical_features_not_recomputable_due_to_missing_bars": mismatch_count - recomputable,
    }
