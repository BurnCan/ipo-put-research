"""Regression tests for event-level M7 dataset limiting."""
import json
import sys
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Company, Filing, IPO, IPOLockup, LockupSignalSnapshot, Security
from app.services.backtest.dataset import build_backtest_dataset
from app.services.event_analysis.constants import SNAPSHOT_VERSION
from scripts import export_lockup_backtest


@pytest.fixture
def backtest_database():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = Session(engine)
    sequence = iter(range(1, 100))

    def add_event(ticker, event_date, offsets, *, classification="classified"):
        company = Company(cik=f"{next(sequence):010d}", name=f"{ticker} Co", ticker=ticker)
        filing = Filing(company=company, form_type="424B4", filed_at=date(2026, 1, 2),
                        accession_number=f"{ticker}-filing", filing_path="filing.txt",
                        sec_url="https://example.test/filing")
        ipo = IPO(company=company, ipo_date=date(2026, 1, 2), ipo_price=10,
                  classification_status=classification,
                  candidate_type="operating_company_ipo", offering_status="priced")
        lockup = IPOLockup(ipo=ipo, filing=filing, holder_group="all", lockup_type="standard",
                           stated_expiration_date=event_date, confidence=.9, parser_name="test",
                           parser_version="1", source_excerpt="test", source_locator="test",
                           evidence_key=f"{ticker}-lockup")
        security = Security(company=company, ticker=ticker, source="test")
        db.add_all((lockup, security)); db.flush()
        ipo.primary_lockup_id = lockup.id
        ipo.primary_lockup_expiration_date = event_date
        for offset in offsets:
            db.add(LockupSignalSnapshot(
                ipo_id=ipo.id, lockup_id=lockup.id, security_id=security.id,
                observation_offset=offset, observation_date=event_date + timedelta(days=offset),
                data_cutoff_date=event_date + timedelta(days=offset), event_date=event_date,
                event_date_source="stated", event_trade_date=event_date,
                snapshot_version=SNAPSHOT_VERSION, trading_sessions_to_event=-offset,
                trading_sessions_since_first_trade=50, available_history_sessions=50,
                close=10, post_ipo_high_to_date=12, post_ipo_low_to_date=8))
        return lockup

    # This chronologically first event must not consume the limit after filters.
    rejected = add_event("BAD", date(2026, 5, 1), (-40, -20), classification="unclassified")
    first = add_event("ONE", date(2026, 6, 1), (-40, -20, -10, -5, -2, -1))
    second = add_event("TWO", date(2026, 7, 1), (-20, -5, -1))
    third = add_event("THR", date(2026, 8, 1), (-10, -1))
    db.commit()
    yield db, rejected, first, second, third
    db.close()


def test_limit_one_returns_complete_first_eligible_event(backtest_database):
    db, rejected, first, *_ = backtest_database
    rows = build_backtest_dataset(db, limit=1)

    assert {row["lockup_id"] for row in rows} == {first.id}
    assert [row["observation_offset"] for row in rows] == [-40, -20, -10, -5, -2, -1]
    assert rejected.id not in {row["lockup_id"] for row in rows}


def test_limit_two_returns_every_offset_for_exactly_two_events(backtest_database):
    db, _, first, second, third = backtest_database
    rows = build_backtest_dataset(db, limit=2)

    assert {row["lockup_id"] for row in rows} == {first.id, second.id}
    assert third.id not in {row["lockup_id"] for row in rows}
    assert len(rows) == 9
    assert {row["observation_offset"] for row in rows if row["lockup_id"] == first.id} == {
        -40, -20, -10, -5, -2, -1}
    assert {row["observation_offset"] for row in rows if row["lockup_id"] == second.id} == {
        -20, -5, -1}


def test_export_reports_event_level_limited_count(backtest_database, monkeypatch, tmp_path, capsys):
    db, _, first, second, _ = backtest_database
    monkeypatch.setattr(export_lockup_backtest, "SessionLocal", lambda: db)
    output = tmp_path / "limited.csv"
    monkeypatch.setattr(sys, "argv", ["export_lockup_backtest.py", "--limit", "2",
                                      "--output", str(output)])

    export_lockup_backtest.main()

    report = json.loads(capsys.readouterr().out)
    assert report["n_events"] == 2
    assert report["n_observations"] == 9
    assert len({row["lockup_id"] for row in build_backtest_dataset(db, limit=2)}) == 2
    assert {first.id, second.id} == {
        row["lockup_id"] for row in build_backtest_dataset(db, limit=2)}
