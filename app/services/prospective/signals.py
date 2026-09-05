"""Immutable M8 strict and secondary shadow prospective tracking."""
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime

from sqlalchemy import select

from app.models import (Company, DailyPrice, IPO, IPOLockup, LockupEventAnalysis,
                        LockupProspectiveSignal, LockupSignalSnapshot, Security)
from app.services.backtest.analysis import FROZEN_HYPOTHESES
from app.services.event_analysis.constants import OUTCOME_VERSION, SNAPSHOT_VERSION_V2
from app.services.event_analysis.lockup_snapshots import compute_snapshot
from app.services.market_calendar import resolve_event_session, resolve_observation_session
from app.services.market_data.coverage import feature_window_coverage

STRICT = "strict_prospective"
SHADOW = "shadow_prospective"
LEGACY_STRICT = "prospective"
OBSERVATION_BEFORE_PROSPECTIVE_START = "observation_before_prospective_start"


@dataclass
class ProspectiveReport:
    hypothesis_id: str
    evaluation_mode: str
    ipos_seen: int = 0
    lockups_seen: int = 0
    eligible_events: int = 0
    pending_observation: int = 0
    waiting_for_market_data: int = 0
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
    calendar_snapshot_mismatches: int = 0
    errors: int = 0
    shadow_eligible_events: int = 0
    shadow_signals_created: int = 0
    shadow_signals_existing: int = 0
    shadow_already_current: int = 0
    shadow_missing_t5_market_data: int = 0
    shadow_incomplete_feature_window: int = 0
    shadow_missed_lock_window: int = 0
    shadow_awaiting_event: int = 0
    shadow_awaiting_outcome: int = 0
    shadow_matured: int = 0
    shadow_outcomes_attached: int = 0
    preview: list[dict] = field(default_factory=list)

    def to_dict(self): return asdict(self)


def _event_date(lockup): return lockup.stated_expiration_date or lockup.calculated_expiration_date


def _existing(db, hypothesis_id, version, lockup_id, mode):
    modes = (STRICT, LEGACY_STRICT) if mode == STRICT else (mode,)
    return db.scalar(select(LockupProspectiveSignal).where(
        LockupProspectiveSignal.hypothesis_id == hypothesis_id,
        LockupProspectiveSignal.hypothesis_version == version,
        LockupProspectiveSignal.lockup_id == lockup_id,
        LockupProspectiveSignal.evaluation_mode.in_(modes)).order_by(LockupProspectiveSignal.id))


def _outcome(db, lockup_id, security_id):
    return db.scalar(select(LockupEventAnalysis).where(
        LockupEventAnalysis.lockup_id == lockup_id,
        LockupEventAnalysis.security_id == security_id,
        LockupEventAnalysis.outcome_version == OUTCOME_VERSION))


def _outcome_observation_date(db, outcome, security_id):
    """Return the frozen +20 session, not the analysis row's later data cutoff."""
    if outcome.event_trade_date is None:
        return outcome.as_of_date
    sessions = list(db.scalars(select(DailyPrice.trade_date).where(
        DailyPrice.security_id == security_id,
        DailyPrice.trade_date >= outcome.event_trade_date,
    ).order_by(DailyPrice.trade_date).limit(21)))
    return sessions[20] if len(sessions) == 21 else outcome.as_of_date


def update_prospective_lockup_signals(db, *, hypothesis_id, evaluation_mode=STRICT,
                                      classification_status="classified",
                                      candidate_type="operating_company_ipo", offering_status="priced",
                                      primary_lockup_only=True, ticker=None, ipo_id=None, limit=None,
                                      dry_run=False, as_of_date=None,
                                      now_utc: datetime | None = None):
    """Advance a mode, optionally using an injected UTC signal-lock timestamp."""
    if (now_utc is not None
            and (now_utc.tzinfo is None or now_utc.utcoffset() != UTC.utcoffset(now_utc))):
        raise ValueError("now_utc must be a timezone-aware UTC datetime")
    if evaluation_mode == LEGACY_STRICT: evaluation_mode = STRICT
    if evaluation_mode not in (STRICT, SHADOW): raise ValueError("unknown evaluation mode")
    if hypothesis_id not in FROZEN_HYPOTHESES: raise ValueError(f"unknown frozen hypothesis: {hypothesis_id}")
    spec = FROZEN_HYPOTHESES[hypothesis_id]
    today = as_of_date or date.today()
    if spec.feature1_threshold is None or spec.feature2_threshold is None or spec.prospective_start_date is None:
        raise ValueError("hypothesis is not frozen for prospective use")
    report = ProspectiveReport(hypothesis_id, evaluation_mode)
    stmt = select(IPOLockup, IPO, Company).join(IPO, IPO.id == IPOLockup.ipo_id).join(Company, Company.id == IPO.company_id)
    if primary_lockup_only: stmt = stmt.where(IPOLockup.id == IPO.primary_lockup_id, IPO.primary_lockup_id.is_not(None))
    if classification_status is not None: stmt = stmt.where(IPO.classification_status == classification_status)
    if candidate_type is not None: stmt = stmt.where(IPO.candidate_type == candidate_type)
    if offering_status is not None: stmt = stmt.where(IPO.offering_status == offering_status)
    if ticker: stmt = stmt.where(Company.ticker.ilike(ticker.strip()))
    if ipo_id is not None: stmt = stmt.where(IPO.id == ipo_id)
    stmt = stmt.order_by(IPO.id, IPOLockup.id)
    if limit is not None: stmt = stmt.limit(limit)
    events = db.execute(stmt).all()
    report.ipos_seen = len({ipo.id for _, ipo, _ in events}); report.lockups_seen = len(events)
    for lockup, ipo, company in events:
        event_date = _event_date(lockup)
        if not event_date: report.unavailable += 1; continue
        # A genuine strict signal is an immutable lock.  Find it before
        # reconstructing eligibility from a snapshot or the current calendar:
        # historical data repairs must never reclassify an already-locked row.
        existing = _existing(db, hypothesis_id, spec.analysis_version, lockup.id,
                             evaluation_mode)
        resolution = resolve_observation_session(event_date, spec.observation_offset)
        snapshot = db.scalar(select(LockupSignalSnapshot).where(
            LockupSignalSnapshot.lockup_id == lockup.id,
            LockupSignalSnapshot.observation_offset == spec.observation_offset,
            LockupSignalSnapshot.snapshot_version == SNAPSHOT_VERSION_V2).order_by(LockupSignalSnapshot.id))
        # Strict retains its existing immutable M6 identity. Shadow deliberately
        # uses only the canonical calendar session and never this legacy identity.
        observation_date = (existing.observation_date if existing is not None else
                            snapshot.observation_date if evaluation_mode == STRICT and snapshot else
                            resolution.observation_session)
        report.calendar_snapshot_mismatches += int(bool(
            snapshot and snapshot.observation_date != resolution.observation_session))
        event_session = resolve_event_session(event_date).event_session
        timing_eligible = (True if existing is not None else
                           observation_date > spec.prospective_start_date if evaluation_mode == STRICT else
                           observation_date <= spec.prospective_start_date < event_session)
        if not timing_eligible:
            # Preserve the original strict lifecycle audit record. It is not a
            # signal and shadow mode can coexist with it under the mode-aware key.
            if (evaluation_mode == STRICT and observation_date <= spec.prospective_start_date
                    and event_session > spec.prospective_start_date):
                lifecycle = _existing(db, hypothesis_id, spec.analysis_version, lockup.id,
                                      "lifecycle_tracking")
                report.unavailable += 1; report.unavailable_observation_before_cutoff += 1
                if lifecycle is None and not dry_run:
                    security = db.scalar(select(Security).where(Security.company_id == ipo.company_id,
                        Security.is_primary.is_(True)).order_by(Security.id))
                    db.add(LockupProspectiveSignal(hypothesis_id=hypothesis_id,
                        hypothesis_version=spec.analysis_version, evaluation_mode="lifecycle_tracking",
                        ipo_id=ipo.id, lockup_id=lockup.id, security_id=security.id if security else None,
                        observation_offset=spec.observation_offset, observation_date=observation_date,
                        required_observation_date=observation_date, calendar_id=resolution.calendar_id,
                        calendar_provider=resolution.calendar_provider, calendar_version=resolution.calendar_version,
                        event_date=event_date, feature1_name=spec.feature1,
                        feature1_threshold=spec.feature1_threshold, feature2_name=spec.feature2,
                        feature2_threshold=spec.feature2_threshold, is_high_high=False,
                        signal_status="unavailable", unavailable_reason=OBSERVATION_BEFORE_PROSPECTIVE_START))
                elif lifecycle is not None: report.already_current += 1
            continue
        report.eligible_events += 1
        if evaluation_mode == SHADOW: report.shadow_eligible_events += 1
        if existing is None and evaluation_mode == SHADOW and today >= event_session:
            report.shadow_missed_lock_window += 1; continue
        security = db.scalar(select(Security).where(Security.company_id == ipo.company_id,
            Security.is_primary.is_(True)).order_by(Security.id))
        if existing is None and security is None:
            if evaluation_mode == SHADOW: report.shadow_missing_t5_market_data += 1
            else: report.waiting_for_market_data += 1
            continue
        values = None
        if existing is None and evaluation_mode == STRICT:
            # A materialized M6 snapshot at the strict identity is authoritative
            # even when an injected market as-of date precedes it.  Pending means
            # that the required observation has not materialized, not that a
            # valid frozen snapshot should be ignored.
            if snapshot is None:
                if observation_date > today: report.pending_observation += 1
                else: report.waiting_for_market_data += 1
                continue
            if (snapshot.observation_date != observation_date
                    or snapshot.snapshot_status != "complete"):
                report.waiting_for_market_data += 1; continue
            values = (getattr(snapshot, spec.feature1), getattr(snapshot, spec.feature2))
        elif existing is None:
            t5 = db.scalar(select(DailyPrice).where(DailyPrice.security_id == security.id,
                                                    DailyPrice.trade_date == observation_date))
            if t5 is None: report.shadow_missing_t5_market_data += 1; continue
            window = feature_window_coverage(db, security, observation_date)
            if not window.complete: report.shadow_incomplete_feature_window += 1; continue
            bars = list(db.scalars(select(DailyPrice).where(
                DailyPrice.security_id == security.id,
                DailyPrice.trade_date.in_(window.required_sessions)).order_by(DailyPrice.trade_date)))
            computed = compute_snapshot(bars, ipo, lockup, observation_offset=spec.observation_offset,
                event_date=event_date, event_date_source="canonical", event_trade_date=event_session)
            values = (computed[spec.feature1], computed[spec.feature2])
        if existing is None and (values[0] is None or values[1] is None):
            if evaluation_mode == SHADOW: report.shadow_incomplete_feature_window += 1
            else: report.waiting_for_market_data += 1
            continue
        if existing is None:
            signal_locked_at = now_utc or datetime.now(UTC)
            # Shadow admission is date-level: durable lock provenance must be
            # strictly earlier than the canonical event session.
            if evaluation_mode == SHADOW and signal_locked_at.date() >= event_session:
                report.shadow_missed_lock_window += 1
                continue
            v1, v2 = map(float, values); s1 = "low" if v1 <= spec.feature1_threshold else "high"; s2 = "low" if v2 <= spec.feature2_threshold else "high"
            report.preview.append({"ticker": company.ticker, "lockup_id": lockup.id,
                "canonical_t5": observation_date, "event_session": event_session,
                spec.feature1: v1, spec.feature2: v2, "interaction_group": f"{s1}_{s2}",
                "eligibility_reason": evaluation_mode})
            report.signals_would_create += int(dry_run)
            if dry_run: continue
            existing = LockupProspectiveSignal(hypothesis_id=hypothesis_id,
                hypothesis_version=spec.analysis_version, evaluation_mode=evaluation_mode,
                ipo_id=ipo.id, lockup_id=lockup.id, security_id=security.id,
                observation_offset=spec.observation_offset, observation_date=observation_date,
                required_observation_date=observation_date, calendar_id=resolution.calendar_id,
                calendar_provider=resolution.calendar_provider, calendar_version=resolution.calendar_version,
                event_date=event_date, event_trade_date=event_session, feature1_name=spec.feature1,
                feature1_value=v1, feature1_threshold=spec.feature1_threshold, feature2_name=spec.feature2,
                feature2_value=v2, feature2_threshold=spec.feature2_threshold, feature1_side=s1,
                feature2_side=s2, interaction_group=f"{s1}_{s2}", is_high_high=s1 == s2 == "high",
                signal_status="signal_created", created_at=signal_locked_at)
            db.add(existing); db.flush(); report.signals_created += 1
            if evaluation_mode == SHADOW: report.shadow_signals_created += 1
        else:
            report.signals_existing += 1
            if evaluation_mode == SHADOW: report.shadow_signals_existing += 1
        outcome = _outcome(db, lockup.id, existing.security_id)
        if existing.realized_outcome_value is not None:
            report.matured += 1; report.already_current += 1
            if evaluation_mode == SHADOW: report.shadow_matured += 1; report.shadow_already_current += 1
            continue
        if today < event_session: status = "awaiting_event"
        elif outcome is None or getattr(outcome, spec.outcome) is None: status = "awaiting_outcome"
        else: status = "matured"
        if status == "matured":
            report.outcomes_would_attach += int(dry_run)
            if not dry_run:
                existing.realized_outcome_name = spec.outcome; existing.realized_outcome_value = getattr(outcome, spec.outcome)
                existing.bearish_mfe_20d = outcome.bearish_mfe_20d; existing.bearish_mae_20d = outcome.bearish_mae_20d
                existing.outcome_observation_date = _outcome_observation_date(
                    db, outcome, existing.security_id)
                existing.outcome_attached_at = datetime.now(UTC)
                existing.signal_status = "matured"; report.outcomes_attached += 1; report.matured += 1
                if evaluation_mode == SHADOW: report.shadow_outcomes_attached += 1; report.shadow_matured += 1
        else:
            if not dry_run: existing.signal_status = status
            setattr(report, status, getattr(report, status) + 1)
            if evaluation_mode == SHADOW: setattr(report, "shadow_" + status, getattr(report, "shadow_" + status) + 1)
    if dry_run: db.rollback()
    else: db.commit()
    return report
