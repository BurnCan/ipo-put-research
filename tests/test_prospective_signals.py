"""Offline regression tests for the M8 prospective cutoff boundary."""
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import (Company, DailyPrice, Filing, IPO, IPOLockup, LockupProspectiveSignal,
                        LockupSignalSnapshot, Security)
from app.services.event_analysis.constants import SNAPSHOT_VERSION
from app.services.backtest.analysis import FROZEN_HYPOTHESES
from app.services.prospective.signals import (_outcome_observation_date,
                                              update_prospective_lockup_signals)
from app.services.prospective.evaluation import evaluate_prospective_signals
from app.services.schema_upgrade import upgrade_milestone_8
from app.services.event_analysis.lockup_snapshots import compute_snapshot
from app.services.market_calendar import resolve_event_session, resolve_observation_session, session_offset


HYPOTHESIS_ID = "m7_return20_vol20_minus5_post20"
CUTOFF = date(2026, 8, 23)


def _database_with_snapshot(observation_date):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = Session(engine)
    company = Company(cik="0000000027", name="Boundary Co", ticker="BDY")
    filing = Filing(company=company, form_type="424B4", filed_at=date(2026, 1, 2),
                    accession_number="boundary-filing", filing_path="filing.txt",
                    sec_url="https://example.test/filing")
    ipo = IPO(company=company, ipo_date=date(2026, 1, 2), ipo_price=10,
              classification_status="classified", candidate_type="operating_company_ipo",
              offering_status="priced")
    event_date = observation_date + timedelta(days=7)
    lockup = IPOLockup(ipo=ipo, filing=filing, holder_group="all", lockup_type="standard",
                       stated_expiration_date=event_date, confidence=.9, parser_name="test",
                       parser_version="1", source_excerpt="test", source_locator="test",
                       evidence_key="boundary-lockup")
    security = Security(company=company, ticker="BDY", source="test")
    db.add_all((lockup, security))
    db.flush()
    ipo.primary_lockup_id = lockup.id
    ipo.primary_lockup_expiration_date = event_date
    db.add(LockupSignalSnapshot(
        ipo_id=ipo.id, lockup_id=lockup.id, security_id=security.id,
        observation_offset=-5, observation_date=observation_date,
        data_cutoff_date=observation_date, event_date=event_date,
        event_date_source="stated", event_trade_date=event_date,
        snapshot_version=SNAPSHOT_VERSION, trading_sessions_to_event=5,
        trading_sessions_since_first_trade=50, available_history_sessions=50,
        close=10, return_20d=.04, realized_vol_20d=.9,
        post_ipo_high_to_date=12, post_ipo_low_to_date=8))
    db.commit()
    return db


@pytest.mark.parametrize(("observation_date", "expected_created", "expected_unavailable"), [
    pytest.param(CUTOFF - timedelta(days=1), 0, 1, id="before-cutoff-is-historical"),
    pytest.param(CUTOFF, 0, 1, id="cutoff-date-is-historical"),
    pytest.param(CUTOFF + timedelta(days=1), 1, 0, id="after-cutoff-is-prospective"),
])
def test_prospective_cutoff_is_exclusive(observation_date, expected_created,
                                         expected_unavailable):
    db = _database_with_snapshot(observation_date)
    try:
        report = update_prospective_lockup_signals(
            db, hypothesis_id=HYPOTHESIS_ID, as_of_date=CUTOFF)

        assert report.signals_created == expected_created
        assert report.unavailable == expected_unavailable
        expected_tracking = expected_created + expected_unavailable
        assert db.scalar(select(func.count()).select_from(LockupProspectiveSignal)) == expected_tracking
        if expected_unavailable:
            row = db.scalar(select(LockupProspectiveSignal))
            assert row.signal_status == "unavailable"
            assert row.unavailable_reason == "observation_before_prospective_start"
            assert row.feature1_value is None and row.feature2_value is None
            assert row.evaluation_mode == "lifecycle_tracking"
            assert report.unavailable_observation_before_cutoff == 1
            assert evaluate_prospective_signals(
                db, hypothesis_id=HYPOTHESIS_ID)["total_signals"] == 0
    finally:
        db.close()


def test_unavailable_tracking_is_idempotent_and_not_a_prospective_signal():
    db = _database_with_snapshot(CUTOFF)
    try:
        first = update_prospective_lockup_signals(db, hypothesis_id=HYPOTHESIS_ID)
        second = update_prospective_lockup_signals(db, hypothesis_id=HYPOTHESIS_ID)

        assert first.unavailable_observation_before_cutoff == 1
        assert second.unavailable_observation_before_cutoff == 1
        assert second.already_current == 1
        assert db.scalar(select(func.count()).select_from(LockupProspectiveSignal)) == 1
        assert evaluate_prospective_signals(
            db, hypothesis_id=HYPOTHESIS_ID)["total_signals"] == 0
    finally:
        db.close()


def test_hypothesis_version_lockup_identity_is_unique_within_evaluation_mode():
    db = _database_with_snapshot(CUTOFF)
    try:
        update_prospective_lockup_signals(db, hypothesis_id=HYPOTHESIS_ID)
        original = db.scalar(select(LockupProspectiveSignal))
        strict = LockupProspectiveSignal(
            hypothesis_id=original.hypothesis_id,
            hypothesis_version=original.hypothesis_version,
            ipo_id=original.ipo_id, lockup_id=original.lockup_id,
            security_id=original.security_id, observation_offset=-5,
            observation_date=CUTOFF + timedelta(days=1), event_date=original.event_date,
            feature1_name=original.feature1_name, feature1_threshold=original.feature1_threshold,
            feature2_name=original.feature2_name, feature2_threshold=original.feature2_threshold,
            is_high_high=False, signal_status="signal_created",
            evaluation_mode="strict_prospective")
        shadow = LockupProspectiveSignal(
            hypothesis_id=original.hypothesis_id,
            hypothesis_version=original.hypothesis_version,
            ipo_id=original.ipo_id, lockup_id=original.lockup_id,
            security_id=original.security_id, observation_offset=-5,
            observation_date=CUTOFF - timedelta(days=5), event_date=original.event_date,
            feature1_name=original.feature1_name, feature1_threshold=original.feature1_threshold,
            feature2_name=original.feature2_name, feature2_threshold=original.feature2_threshold,
            is_high_high=False, signal_status="signal_created",
            evaluation_mode="shadow_prospective")
        db.add_all((strict, shadow))
        db.commit()

        assert db.scalar(select(func.count()).select_from(LockupProspectiveSignal)) == 3

        duplicate_strict = LockupProspectiveSignal(
            hypothesis_id=strict.hypothesis_id, hypothesis_version=strict.hypothesis_version,
            ipo_id=strict.ipo_id, lockup_id=strict.lockup_id, security_id=strict.security_id,
            observation_offset=-5, observation_date=strict.observation_date,
            event_date=strict.event_date, feature1_name=strict.feature1_name,
            feature1_threshold=strict.feature1_threshold, feature2_name=strict.feature2_name,
            feature2_threshold=strict.feature2_threshold, is_high_high=False,
            signal_status="signal_created", evaluation_mode="strict_prospective")
        db.add(duplicate_strict)

        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        assert db.scalar(select(func.count()).select_from(LockupProspectiveSignal)) == 3
    finally:
        db.close()


def test_absent_snapshot_uses_calendar_and_remains_pending_before_t5():
    db = _database_with_snapshot(CUTOFF)
    try:
        snapshot = db.scalar(select(LockupSignalSnapshot))
        db.delete(snapshot)
        db.commit()

        report = update_prospective_lockup_signals(
            db, hypothesis_id=HYPOTHESIS_ID, as_of_date=CUTOFF)

        assert report.pending_observation == 1
        assert report.unavailable == 0
        assert db.scalar(select(func.count()).select_from(LockupProspectiveSignal)) == 0
    finally:
        db.close()


def test_future_event_with_missed_calendar_t5_is_unavailable_without_bars():
    db = _database_with_snapshot(CUTOFF + timedelta(days=1))
    try:
        snapshot = db.scalar(select(LockupSignalSnapshot))
        lockup = db.get(IPOLockup, snapshot.lockup_id)
        ipo = db.get(IPO, snapshot.ipo_id)
        db.delete(snapshot)
        lockup.stated_expiration_date = date(2026, 8, 28)
        ipo.primary_lockup_expiration_date = lockup.stated_expiration_date
        db.commit()

        report = update_prospective_lockup_signals(
            db, hypothesis_id=HYPOTHESIS_ID, as_of_date=CUTOFF)
        row = db.scalar(select(LockupProspectiveSignal))

        assert report.unavailable_observation_before_cutoff == 1
        assert row.signal_status == "unavailable"
        assert row.required_observation_date == date(2026, 8, 21)
        assert row.calendar_id == "XNYS"
        assert row.calendar_provider == "exchange_calendars"
        assert row.calendar_version
    finally:
        db.close()


def test_reached_t5_without_snapshot_waits_for_market_data():
    db = _database_with_snapshot(CUTOFF + timedelta(days=1))
    try:
        snapshot = db.scalar(select(LockupSignalSnapshot))
        lockup = db.get(IPOLockup, snapshot.lockup_id)
        ipo = db.get(IPO, snapshot.ipo_id)
        db.delete(snapshot)
        lockup.stated_expiration_date = date(2026, 9, 1)
        ipo.primary_lockup_expiration_date = lockup.stated_expiration_date
        db.commit()

        report = update_prospective_lockup_signals(
            db, hypothesis_id=HYPOTHESIS_ID, as_of_date=date(2026, 8, 25))

        assert report.waiting_for_market_data == 1
        assert report.pending_observation == 0
        assert db.scalar(select(func.count()).select_from(LockupProspectiveSignal)) == 0
    finally:
        db.close()


def test_existing_genuine_signal_is_authoritative_over_historical_snapshot():
    db = _database_with_snapshot(CUTOFF)
    try:
        snapshot = db.scalar(select(LockupSignalSnapshot))
        snapshot.return_20d = .5
        snapshot.realized_vol_20d = .1
        frozen = FROZEN_HYPOTHESES[HYPOTHESIS_ID]
        row = LockupProspectiveSignal(
            hypothesis_id=HYPOTHESIS_ID, hypothesis_version=frozen.analysis_version,
            ipo_id=snapshot.ipo_id, lockup_id=snapshot.lockup_id,
            security_id=snapshot.security_id, observation_offset=-5,
            observation_date=CUTOFF + timedelta(days=1), event_date=snapshot.event_date,
            event_trade_date=snapshot.event_trade_date, feature1_name=frozen.feature1,
            feature1_value=.04, feature1_threshold=frozen.feature1_threshold,
            feature2_name=frozen.feature2, feature2_value=.9,
            feature2_threshold=frozen.feature2_threshold, feature1_side="low",
            feature2_side="high", interaction_group="low_high", is_high_high=False,
            signal_status="awaiting_event", evaluation_mode="prospective")
        db.add(row)
        db.commit()

        report = update_prospective_lockup_signals(db, hypothesis_id=HYPOTHESIS_ID)

        assert report.signals_existing == 1
        assert report.unavailable == 0
        assert row.observation_date == CUTOFF + timedelta(days=1)
        assert float(row.feature1_value) == pytest.approx(.04)
        assert float(row.feature2_value) == pytest.approx(.9)
        assert row.interaction_group == "low_high"
        assert row.signal_status != "unavailable"
    finally:
        db.close()


def test_signal_lock_timestamp_is_populated_once_on_rerun():
    db = _database_with_snapshot(CUTOFF + timedelta(days=1))
    try:
        first = update_prospective_lockup_signals(
            db, hypothesis_id=HYPOTHESIS_ID, as_of_date=CUTOFF + timedelta(days=1))
        row = db.scalar(select(LockupProspectiveSignal))
        locked_at = row.created_at

        second = update_prospective_lockup_signals(
            db, hypothesis_id=HYPOTHESIS_ID, as_of_date=CUTOFF + timedelta(days=2))
        db.refresh(row)

        assert first.signals_created == 1
        assert locked_at is not None
        assert second.signals_existing == 1
        assert row.created_at == locked_at
    finally:
        db.close()


def _database_with_shadow_timing():
    """Return a deterministic pre-freeze T-5 / post-freeze event fixture."""
    db = _database_with_snapshot(date(2026, 8, 18))
    snapshot = db.scalar(select(LockupSignalSnapshot))
    lockup, ipo = db.get(IPOLockup, snapshot.lockup_id), db.get(IPO, snapshot.ipo_id)
    event_date = date(2026, 8, 25)
    lockup.stated_expiration_date = event_date
    ipo.primary_lockup_expiration_date = event_date
    snapshot.event_date = event_date
    snapshot.event_trade_date = event_date
    db.commit()
    assert resolve_observation_session(event_date, -5).observation_session == date(2026, 8, 18)
    return db


def test_shadow_is_not_created_on_or_after_canonical_event_session():
    db = _database_with_shadow_timing()
    try:
        lockup = db.scalar(select(IPOLockup))
        event_session = resolve_event_session(lockup.stated_expiration_date).event_session
        report = update_prospective_lockup_signals(
            db, hypothesis_id=HYPOTHESIS_ID, evaluation_mode="shadow_prospective",
            as_of_date=event_session)

        assert report.shadow_missed_lock_window == 1
        assert db.scalar(select(func.count()).select_from(LockupProspectiveSignal)) == 0
    finally:
        db.close()


def test_shadow_exact_window_features_equal_m6_snapshot_computation():
    db = _database_with_shadow_timing()
    try:
        snapshot = db.scalar(select(LockupSignalSnapshot))
        lockup, ipo = db.get(IPOLockup, snapshot.lockup_id), db.get(IPO, snapshot.ipo_id)
        event_session = resolve_event_session(lockup.stated_expiration_date).event_session
        observation = resolve_observation_session(
            lockup.stated_expiration_date, -5).observation_session
        sessions = [session_offset(observation, offset) for offset in range(-20, 1)]
        bars = []
        for index, trade_date in enumerate(sessions):
            close = 10 + index * .1 + (index % 2) * .03
            bars.append(DailyPrice(
                security_id=snapshot.security_id, trade_date=trade_date,
                open=close - .02, high=close + .08, low=close - .08, close=close,
                volume=1000 + index * 17, provider="test", provider_symbol="BDY"))
        db.add_all(bars)
        computed = compute_snapshot(
            bars, ipo, lockup, observation_offset=-5,
            event_date=lockup.stated_expiration_date, event_date_source="canonical",
            event_trade_date=event_session)
        snapshot.observation_date = observation
        snapshot.data_cutoff_date = observation
        snapshot.return_20d = computed["return_20d"]
        snapshot.realized_vol_20d = computed["realized_vol_20d"]
        db.commit()

        report = update_prospective_lockup_signals(
            db, hypothesis_id=HYPOTHESIS_ID, evaluation_mode="shadow_prospective",
            as_of_date=CUTOFF)
        signal = db.scalar(select(LockupProspectiveSignal).where(
            LockupProspectiveSignal.evaluation_mode == "shadow_prospective"))

        assert report.shadow_signals_created == 1
        assert float(signal.feature1_value) == pytest.approx(float(snapshot.return_20d))
        assert float(signal.feature2_value) == pytest.approx(float(snapshot.realized_vol_20d))
        assert signal.created_at.date() < event_session
    finally:
        db.close()


def test_m8_upgrade_normalizes_legacy_strict_mode_and_is_idempotent():
    db = _database_with_snapshot(CUTOFF + timedelta(days=1))
    try:
        update_prospective_lockup_signals(db, hypothesis_id=HYPOTHESIS_ID)
        row = db.scalar(select(LockupProspectiveSignal))
        db.execute(text(
            "UPDATE lockup_prospective_signals SET evaluation_mode = 'prospective'"))
        db.commit()

        first = upgrade_milestone_8(db.bind)
        second = upgrade_milestone_8(db.bind)
        db.expire_all()

        assert row.evaluation_mode == "strict_prospective"
        assert first == ["lockup_prospective_signals.evaluation_mode_values"]
        assert second == []
        assert db.scalar(select(func.count()).select_from(LockupProspectiveSignal)) == 1
        default = next(column for column in inspect(db.bind).get_columns(
            "lockup_prospective_signals") if column["name"] == "evaluation_mode")["default"]
        assert "strict_prospective" in default
    finally:
        db.close()


def test_strict_outcome_observation_date_remains_exact_post_20_session():
    db = _database_with_snapshot(CUTOFF + timedelta(days=1))
    try:
        security = db.scalar(select(Security))
        event_session = date(2026, 9, 1)
        sessions = [session_offset(event_session, offset) for offset in range(21)]
        db.add_all(DailyPrice(
            security_id=security.id, trade_date=trade_date, open=10, high=11,
            low=9, close=10, volume=1000, provider="test", provider_symbol="BDY")
            for trade_date in sessions)
        db.commit()
        outcome = SimpleNamespace(
            event_trade_date=event_session, as_of_date=session_offset(event_session, 40))

        assert _outcome_observation_date(db, outcome, security.id) == sessions[20]
        assert _outcome_observation_date(db, outcome, security.id) != outcome.as_of_date
    finally:
        db.close()
