"""Offline regression tests for the M8 prospective cutoff boundary."""
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import (Company, Filing, IPO, IPOLockup, LockupProspectiveSignal,
                        LockupSignalSnapshot, Security)
from app.services.event_analysis.constants import SNAPSHOT_VERSION
from app.services.prospective.signals import update_prospective_lockup_signals


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
        assert db.scalar(select(func.count()).select_from(LockupProspectiveSignal)) == expected_created
    finally:
        db.close()
