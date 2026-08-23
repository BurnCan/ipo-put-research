"""Offline regression tests for the M8 prospective cutoff boundary."""
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import (Company, Filing, IPO, IPOLockup, LockupProspectiveSignal,
                        LockupSignalSnapshot, Security)
from app.services.event_analysis.constants import SNAPSHOT_VERSION
from app.services.backtest.analysis import FROZEN_HYPOTHESES
from app.services.prospective.signals import update_prospective_lockup_signals
from app.services.prospective.evaluation import evaluate_prospective_signals


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
        report = update_prospective_lockup_signals(db, hypothesis_id=HYPOTHESIS_ID)

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


def test_hypothesis_version_lockup_identity_is_unique_across_evaluation_modes():
    db = _database_with_snapshot(CUTOFF)
    try:
        update_prospective_lockup_signals(db, hypothesis_id=HYPOTHESIS_ID)
        original = db.scalar(select(LockupProspectiveSignal))
        duplicate = LockupProspectiveSignal(
            hypothesis_id=original.hypothesis_id,
            hypothesis_version=original.hypothesis_version,
            ipo_id=original.ipo_id, lockup_id=original.lockup_id,
            security_id=original.security_id, observation_offset=-5,
            observation_date=CUTOFF + timedelta(days=1), event_date=original.event_date,
            feature1_name=original.feature1_name, feature1_threshold=original.feature1_threshold,
            feature2_name=original.feature2_name, feature2_threshold=original.feature2_threshold,
            is_high_high=False, signal_status="signal_created",
            evaluation_mode="prospective")
        db.add(duplicate)

        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        assert db.scalar(select(func.count()).select_from(LockupProspectiveSignal)) == 1
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
        assert row.signal_status != "unavailable"
    finally:
        db.close()
