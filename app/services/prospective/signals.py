"""M8 prospective tracking; consumes M6 records without recomputing them."""
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from sqlalchemy import select

from app.models import (Company, DailyPrice, IPO, IPOLockup, LockupEventAnalysis,
                        LockupProspectiveSignal, LockupSignalSnapshot, Security)
from app.services.backtest.analysis import FROZEN_HYPOTHESES
from app.services.event_analysis.constants import OUTCOME_VERSION, SNAPSHOT_VERSION
from app.services.event_analysis.sessions import (align_event_trade_date,
                                                   get_trading_session_offset)

OBSERVATION_BEFORE_PROSPECTIVE_START = "observation_before_prospective_start"


@dataclass
class ProspectiveReport:
    hypothesis_id: str
    ipos_seen: int = 0
    lockups_seen: int = 0
    eligible_events: int = 0
    pending_observation: int = 0
    signals_created: int = 0
    signals_would_create: int = 0
    signals_existing: int = 0
    awaiting_event: int = 0
    awaiting_outcome: int = 0
    outcomes_attached: int = 0
    outcomes_would_attach: int = 0
    matured: int = 0
    already_current: int = 0
    unavailable: int = 0
    unavailable_observation_before_cutoff: int = 0
    errors: int = 0

    def to_dict(self): return asdict(self)


def _event_date(lockup):
    return lockup.stated_expiration_date or lockup.calculated_expiration_date


def _status(row, outcome):
    if row.observation_date is None: return "pending_observation"
    if outcome and outcome.post_20d_return is not None: return "matured"
    if row.event_trade_date is None: return "awaiting_event"
    return "awaiting_outcome"


def _required_observation_date(db, lockup, ipo, snapshot, offset):
    """Resolve the exact session with M6 alignment only; never project a calendar."""
    if snapshot is not None:
        return snapshot.observation_date, snapshot.security_id
    alignment = db.scalar(select(LockupSignalSnapshot).where(
        LockupSignalSnapshot.lockup_id == lockup.id,
        LockupSignalSnapshot.snapshot_version == SNAPSHOT_VERSION,
        LockupSignalSnapshot.event_trade_date.is_not(None)).order_by(LockupSignalSnapshot.id))
    outcome = None
    if alignment is None:
        outcome = db.scalar(select(LockupEventAnalysis).where(
            LockupEventAnalysis.lockup_id == lockup.id,
            LockupEventAnalysis.outcome_version == OUTCOME_VERSION,
            LockupEventAnalysis.event_trade_date.is_not(None)).order_by(LockupEventAnalysis.id))
    security_id = alignment.security_id if alignment else outcome.security_id if outcome else None
    event_trade_date = (alignment.event_trade_date if alignment else
                        outcome.event_trade_date if outcome else None)
    if security_id is None:
        security = db.scalar(select(Security).where(
            Security.company_id == ipo.company_id,
            Security.is_primary.is_(True)).order_by(Security.id))
        security_id = security.id if security else None
    if security_id is None:
        return None, None
    bars = list(db.scalars(select(DailyPrice).where(
        DailyPrice.security_id == security_id).order_by(DailyPrice.trade_date)))
    if event_trade_date is None:
        event_trade_date = align_event_trade_date(bars, _event_date(lockup))
    observation = (get_trading_session_offset(bars, event_trade_date, offset)
                   if event_trade_date is not None else None)
    return (observation.trade_date if observation else None), security_id


def _unavailable_row(spec, hypothesis_id, ipo, lockup, event_date,
                     observation_date, security_id):
    return LockupProspectiveSignal(
        hypothesis_id=hypothesis_id, hypothesis_version=spec.analysis_version,
        ipo_id=ipo.id, lockup_id=lockup.id, security_id=security_id,
        observation_offset=spec.observation_offset, observation_date=observation_date,
        event_date=event_date, feature1_name=spec.feature1,
        feature1_threshold=spec.feature1_threshold, feature2_name=spec.feature2,
        feature2_threshold=spec.feature2_threshold, is_high_high=False,
        signal_status="unavailable", unavailable_reason=OBSERVATION_BEFORE_PROSPECTIVE_START,
        evaluation_mode="lifecycle_tracking")


def update_prospective_lockup_signals(db, *, hypothesis_id, classification_status="classified",
                                      candidate_type="operating_company_ipo",
                                      offering_status="priced", primary_lockup_only=True,
                                      ticker=None, ipo_id=None, limit=None, dry_run=False):
    """Advance prospective records while never rewriting an existing signal decision."""
    if hypothesis_id not in FROZEN_HYPOTHESES: raise ValueError(f"unknown frozen hypothesis: {hypothesis_id}")
    spec = FROZEN_HYPOTHESES[hypothesis_id]
    if spec.feature1_threshold is None or spec.feature2_threshold is None or spec.prospective_start_date is None:
        raise ValueError("hypothesis is not frozen for prospective use")
    report = ProspectiveReport(hypothesis_id)
    stmt = (select(IPOLockup, IPO).join(IPO, IPO.id == IPOLockup.ipo_id)
            .join(Company, Company.id == IPO.company_id))
    if primary_lockup_only: stmt = stmt.where(IPOLockup.id == IPO.primary_lockup_id,
        IPO.primary_lockup_id.is_not(None), IPO.primary_lockup_expiration_date.is_not(None))
    if classification_status is not None: stmt = stmt.where(IPO.classification_status == classification_status)
    if candidate_type is not None: stmt = stmt.where(IPO.candidate_type == candidate_type)
    if offering_status is not None: stmt = stmt.where(IPO.offering_status == offering_status)
    if ticker: stmt = stmt.where(Company.ticker.ilike(ticker.strip()))
    if ipo_id is not None: stmt = stmt.where(IPO.id == ipo_id)
    stmt = stmt.order_by(IPO.id, IPOLockup.id)
    if limit is not None: stmt = stmt.limit(limit)
    events = db.execute(stmt).all()
    report.ipos_seen = len({ipo.id for _, ipo in events}); report.lockups_seen = len(events)
    for lockup, ipo in events:
        event_date = _event_date(lockup)
        if event_date is None: report.unavailable += 1; continue
        report.eligible_events += 1
        existing = db.scalar(select(LockupProspectiveSignal).where(
            LockupProspectiveSignal.hypothesis_id == hypothesis_id,
            LockupProspectiveSignal.hypothesis_version == spec.analysis_version,
            LockupProspectiveSignal.lockup_id == lockup.id))
        snapshot = db.scalar(select(LockupSignalSnapshot).where(
            LockupSignalSnapshot.lockup_id == lockup.id,
            LockupSignalSnapshot.observation_offset == spec.observation_offset,
            LockupSignalSnapshot.snapshot_version == SNAPSHOT_VERSION).order_by(LockupSignalSnapshot.id))
        # Any previously frozen prospective evidence wins over later lifecycle
        # reconstruction, including an older M6 snapshot.
        if existing is not None and existing.signal_status == "unavailable":
            report.unavailable += 1
            report.unavailable_observation_before_cutoff += int(
                existing.unavailable_reason == OBSERVATION_BEFORE_PROSPECTIVE_START)
            report.already_current += 1
            continue
        required_date, required_security_id = _required_observation_date(
            db, lockup, ipo, snapshot, spec.observation_offset)
        # Observations through the hypothesis freeze date belong to discovery,
        # never prospective; only strictly later snapshots are out-of-sample.
        if existing is None and required_date is not None and required_date <= spec.prospective_start_date:
            report.unavailable += 1
            if event_date > spec.prospective_start_date:
                report.unavailable_observation_before_cutoff += 1
                if not dry_run:
                    db.add(_unavailable_row(spec, hypothesis_id, ipo, lockup, event_date,
                                            required_date, required_security_id))
                    db.flush()
            continue
        if existing is None and (snapshot is None or getattr(snapshot, spec.feature1) is None or
                                 getattr(snapshot, spec.feature2) is None):
            report.pending_observation += 1; continue
        if existing is None:
            report.signals_would_create += int(dry_run)
            if dry_run: continue
            v1, v2 = float(getattr(snapshot, spec.feature1)), float(getattr(snapshot, spec.feature2))
            s1 = "low" if v1 <= spec.feature1_threshold else "high"
            s2 = "low" if v2 <= spec.feature2_threshold else "high"
            existing = LockupProspectiveSignal(
                hypothesis_id=hypothesis_id, hypothesis_version=spec.analysis_version,
                ipo_id=ipo.id, lockup_id=lockup.id, security_id=snapshot.security_id,
                observation_offset=spec.observation_offset, observation_date=snapshot.observation_date,
                event_date=event_date, event_trade_date=snapshot.event_trade_date,
                feature1_name=spec.feature1, feature1_value=v1, feature1_threshold=spec.feature1_threshold,
                feature2_name=spec.feature2, feature2_value=v2, feature2_threshold=spec.feature2_threshold,
                feature1_side=s1, feature2_side=s2, interaction_group=f"{s1}_{s2}",
                is_high_high=s1 == s2 == "high", signal_status="signal_created")
            db.add(existing); db.flush(); report.signals_created += 1
        else: report.signals_existing += 1
        outcome = db.scalar(select(LockupEventAnalysis).where(
            LockupEventAnalysis.lockup_id == lockup.id,
            LockupEventAnalysis.security_id == existing.security_id,
            LockupEventAnalysis.outcome_version == OUTCOME_VERSION))
        if existing.realized_outcome_value is not None:
            report.matured += 1; report.already_current += 1; continue
        status = _status(existing, outcome)
        if status == "matured":
            report.outcomes_would_attach += int(dry_run)
            if not dry_run:
                existing.realized_outcome_name = spec.outcome
                existing.realized_outcome_value = getattr(outcome, spec.outcome)
                existing.bearish_mfe_20d = outcome.bearish_mfe_20d
                existing.bearish_mae_20d = outcome.bearish_mae_20d
                bars = list(db.scalars(select(DailyPrice).where(
                    DailyPrice.security_id == existing.security_id,
                    DailyPrice.trade_date >= outcome.event_trade_date).order_by(DailyPrice.trade_date).limit(21)))
                existing.outcome_observation_date = bars[20].trade_date if len(bars) > 20 else outcome.as_of_date
                existing.outcome_attached_at = datetime.now(UTC)
                existing.signal_status = "matured"; report.outcomes_attached += 1; report.matured += 1
        else:
            if not dry_run: existing.signal_status = status
            setattr(report, status, getattr(report, status) + 1)
    if dry_run: db.rollback()
    else: db.commit()
    return report
