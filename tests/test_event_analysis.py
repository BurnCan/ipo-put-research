"""Offline formula and session tests for the Milestone 6 analysis layer."""
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import (Company, DailyPrice, Filing, IPO, IPOLockup, LockupSignalSnapshot,
                        Security)
from app.services.event_analysis.analysis import (recompute_lockup_analysis,
                                                  recompute_lockup_analyses)
from app.services.event_analysis.constants import SNAPSHOT_OFFSETS
from app.services.event_analysis.lockup_outcomes import compute_event_outcome
from app.services.event_analysis.lockup_snapshots import compute_snapshot
from app.services.event_analysis.sessions import (align_event_trade_date, event_date_with_source,
                                                   get_trading_session_offset)


def bar(day, close, *, open_=None, high=None, low=None, volume=100):
    close = float(close)
    return SimpleNamespace(trade_date=day, open=close if open_ is None else open_,
                           high=close if high is None else high, low=close if low is None else low,
                           close=close, volume=volume)


def weekdays(start, count):
    result, day = [], start
    while len(result) < count:
        if day.weekday() < 5: result.append(day)
        day += timedelta(days=1)
    return result


def analysis_database(event_date, dates):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = Session(engine)
    company = Company(cik="0000000042", name="Session Co", ticker="SES", exchange="NYSE")
    filing = Filing(company=company, form_type="424B4", filed_at=dates[0],
                    accession_number="0000000042-26-000001", filing_path="filing.txt",
                    sec_url="https://example.test/filing")
    ipo = IPO(company=company, ipo_date=dates[0], ipo_price=10, shares_offered=100,
              primary_shares=80, secondary_shares=20, deal_size=1000)
    lockup = IPOLockup(ipo=ipo, filing=filing, holder_group="all", lockup_type="standard",
                       duration_days=180, stated_expiration_date=event_date, confidence=.9,
                       parser_name="test", parser_version="1", source_excerpt="test",
                       source_locator="test", evidence_key=f"event-{event_date}")
    security = Security(company=company, ticker="SES", exchange="NYSE", is_primary=True,
                        source="test")
    db.add_all([lockup, security])
    db.flush()
    db.add_all([
        DailyPrice(security_id=security.id, trade_date=day, open=10 + index,
                   high=11 + index, low=9 + index, close=10 + index, volume=1000,
                   provider="test", provider_symbol="SES")
        for index, day in enumerate(dates)
    ])
    db.commit()
    return db, lockup, security


@pytest.fixture
def selection_database(monkeypatch):
    """Small offline universe whose derived computation is replaced by an ID recorder."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = Session(engine)
    sequence = iter(range(1, 100))

    def add_ipo(ticker, *, classification="classified", candidate="operating_company_ipo",
                offering="priced", primary=True, dated=True, extra=False):
        company = Company(cik=f"{next(sequence):010d}", name=f"{ticker} Co", ticker=ticker)
        ipo = IPO(company=company, ipo_date=date(2026, 1, 2), ipo_price=10,
                  classification_status=classification, candidate_type=candidate,
                  offering_status=offering)
        filing = Filing(company=company, form_type="424B4", filed_at=date(2026, 1, 2),
                        accession_number=f"{ticker}-filing", filing_path="filing.txt",
                        sec_url="https://example.test/filing")
        lockup = IPOLockup(ipo=ipo, filing=filing, holder_group="all", lockup_type="standard",
                           stated_expiration_date=date(2026, 7, 1), confidence=.9,
                           parser_name="test", parser_version="1", source_excerpt="test",
                           source_locator="test", evidence_key=f"{ticker}-primary")
        db.add(lockup); db.flush()
        if primary:
            ipo.primary_lockup_id = lockup.id
            ipo.primary_lockup_expiration_date = date(2026, 7, 1) if dated else None
        other = None
        if extra:
            other = IPOLockup(ipo=ipo, filing=filing, holder_group="employees",
                              lockup_type="other", stated_expiration_date=date(2026, 8, 1),
                              confidence=.8, parser_name="test", parser_version="1",
                              source_excerpt="other", source_locator="test",
                              evidence_key=f"{ticker}-other")
            db.add(other); db.flush()
        return ipo, lockup, other

    records = {
        "unclassified": add_ipo("AAA", classification="unclassified"),
        "wrong_candidate": add_ipo("BBB", candidate="spac"),
        "wrong_offering": add_ipo("CCC", offering="withdrawn"),
        "no_primary": add_ipo("DDD", primary=False),
        "undated": add_ipo("EEE", dated=False),
        "eligible": add_ipo("FFF", extra=True),
        "eligible_2": add_ipo("GGG"),
    }
    db.commit()
    seen = []

    def record(_db, lockup, *, report, **_kwargs):
        seen.append(lockup.id)
        return report

    monkeypatch.setattr("app.services.event_analysis.analysis.recompute_lockup_analysis", record)
    yield db, records, seen
    db.close()


@pytest.mark.parametrize("argument,value,expected_ticker", [
    ("classification_status", "unclassified", "AAA"),
    ("candidate_type", "spac", "BBB"),
    ("offering_status", "withdrawn", "CCC"),
])
def test_research_value_filters(selection_database, argument, value, expected_ticker):
    db, records, seen = selection_database
    report = recompute_lockup_analyses(db, **{argument: value})
    expected = records[{"AAA": "unclassified", "BBB": "wrong_candidate",
                        "CCC": "wrong_offering"}[expected_ticker]][1]
    assert seen == [expected.id]
    assert report.ipos_seen == report.lockups_seen == 1


def test_primary_lockup_only_requires_selected_and_dated_primary(selection_database):
    db, records, seen = selection_database
    report = recompute_lockup_analyses(db, primary_lockup_only=True)
    expected = {records[name][1].id for name in
                ("unclassified", "wrong_candidate", "wrong_offering", "eligible", "eligible_2")}
    assert set(seen) == expected
    assert records["no_primary"][1].id not in seen
    assert records["undated"][1].id not in seen
    assert report.ipos_seen == 5


def test_all_filters_compose_before_limit(selection_database):
    db, records, seen = selection_database
    report = recompute_lockup_analyses(
        db, classification_status="classified", candidate_type="operating_company_ipo",
        offering_status="priced", primary_lockup_only=True, limit=1)
    # Five earlier IPO rows fail at least one predicate; limiting the broad universe first
    # would therefore return no lockup instead of FFF.
    assert seen == [records["eligible"][1].id]
    assert report.ipos_seen == report.lockups_seen == 1


def test_ticker_and_ipo_id_selectors_still_work(selection_database):
    db, records, seen = selection_database
    recompute_lockup_analyses(db, ticker="fff")
    assert seen == [records["eligible"][1].id]
    seen.clear()
    recompute_lockup_analyses(db, ipo_id=records["eligible_2"][0].id)
    assert seen == [records["eligible_2"][1].id]


def test_explicit_non_primary_lockup_overrides_new_filters(selection_database):
    db, records, seen = selection_database
    ipo, primary, other = records["eligible"]
    recompute_lockup_analyses(
        db, lockup_id=other.id, classification_status="does-not-match",
        candidate_type="does-not-match", offering_status="does-not-match",
        primary_lockup_only=True)
    assert other.id != primary.id
    assert seen == [other.id]


def test_unfiltered_behavior_remains_selected_primary_universe(selection_database):
    db, records, seen = selection_database
    report = recompute_lockup_analyses(db)
    assert set(seen) == {item[1].id for item in records.values() if item[0].primary_lockup_id}
    assert records["eligible"][2].id not in seen
    assert report.ipos_seen == report.lockups_seen == 6


def test_date_provenance_alignment_and_offsets():
    lockup = SimpleNamespace(stated_expiration_date=date(2026, 4, 11),
                             calculated_expiration_date=date(2026, 4, 10))
    assert event_date_with_source(lockup) == (date(2026, 4, 11), "stated")
    lockup.stated_expiration_date = None
    assert event_date_with_source(lockup) == (date(2026, 4, 10), "calculated")
    bars = [bar(date(2026, 4, 10), 10), bar(date(2026, 4, 13), 11), bar(date(2026, 4, 14), 12)]
    assert align_event_trade_date(bars, date(2026, 4, 11)) == date(2026, 4, 13)
    assert align_event_trade_date(bars[:1], date(2026, 4, 11)) is None
    assert get_trading_session_offset(bars, date(2026, 4, 13), -1).trade_date == date(2026, 4, 10)
    assert get_trading_session_offset(bars, date(2026, 4, 13), 1).trade_date == date(2026, 4, 14)


def test_prospective_snapshots_do_not_approximate_a_weekday_holiday():
    dates = [day for day in weekdays(date(2026, 3, 2), 75)
             if day != date(2026, 6, 8) and day <= date(2026, 6, 12)]
    db, lockup, security = analysis_database(date(2026, 6, 16), dates)
    try:
        report = recompute_lockup_analysis(db, lockup, security)
        snapshots = list(db.scalars(select(LockupSignalSnapshot)))
        assert report.snapshots_created == 0
        assert snapshots == []
    finally:
        db.close()


def test_historical_snapshots_use_exact_stored_sessions_across_holiday():
    dates = [day for day in weekdays(date(2026, 2, 2), 100)
             if day != date(2026, 6, 8) and day <= date(2026, 6, 16)]
    db, lockup, security = analysis_database(date(2026, 6, 16), dates)
    try:
        report = recompute_lockup_analysis(db, lockup, security)
        snapshots = list(db.scalars(select(LockupSignalSnapshot).order_by(
            LockupSignalSnapshot.observation_offset)))
        event_index = dates.index(date(2026, 6, 16))
        expected = {offset: dates[event_index + offset] for offset in SNAPSHOT_OFFSETS}
        assert report.snapshots_created == len(SNAPSHOT_OFFSETS)
        assert {row.observation_offset: row.observation_date for row in snapshots} == expected
        assert expected[-10] == date(2026, 6, 1)
    finally:
        db.close()


def test_snapshot_exact_windows_and_no_lookahead():
    dates = weekdays(date(2025, 1, 2), 45)
    bars = [bar(day, i + 10, high=i + 11, low=i + 9, volume=100 + i) for i, day in enumerate(dates)]
    ipo = SimpleNamespace(ipo_date=dates[0], ipo_price=10, primary_shares=80, secondary_shares=20,
                          shares_offered=100, deal_size=1000)
    lockup = SimpleNamespace(duration_days=180, holder_group="all", lockup_type="standard", confidence=.9)
    kwargs = dict(observation_offset=-10, event_date=date(2025, 5, 1), event_date_source="stated",
                  event_trade_date=None)
    snapshot = compute_snapshot(bars[:21], ipo, lockup, **kwargs)
    assert snapshot["return_20d"] == pytest.approx(30 / 10 - 1)
    assert snapshot["return_40d"] is None
    assert snapshot["avg_volume_5d"] == pytest.approx(sum(range(116, 121)) / 5)
    assert snapshot["avg_dollar_volume_5d"] == pytest.approx(
        sum((26 + i) * (116 + i) for i in range(5)) / 5)
    assert snapshot["secondary_share_fraction"] == .2
    # Huge future high/low/close cannot alter the caller's already-cut-off history.
    changed_future = bars + [bar(date(2025, 5, 2), 10000, high=20000, low=1)]
    repeated = compute_snapshot(changed_future[:21], ipo, lockup, **kwargs)
    for field in ("close", "return_20d", "post_ipo_high_to_date", "post_ipo_low_to_date",
                  "drawdown_from_post_ipo_high", "position_in_post_ipo_range"):
        assert repeated[field] == snapshot[field]


def test_outcomes_alliance_event_returns_excursions_volume_and_completeness():
    dates = weekdays(date(2026, 3, 3), 61)
    bars = [bar(day, 20 + i / 100, open_=20 + i / 100, high=21 + i / 100,
                low=19 + i / 100, volume=100) for i, day in enumerate(dates)]
    event_index = 25
    bars[event_index] = bar(date(2026, 4, 7), 22.29, open_=22, high=23, low=21, volume=300)
    # Preserve the real Alliance-style +1 close expectation.
    bars[event_index + 1] = bar(date(2026, 4, 8), 23.56, high=24, low=20, volume=200)
    outcome = compute_event_outcome(bars, date(2026, 4, 7), date(2026, 4, 7))
    assert outcome["post_1d_return"] == pytest.approx(23.56 / 22.29 - 1)
    assert outcome["event_gap_return"] == pytest.approx(22 / float(bars[event_index - 1].close) - 1)
    assert outcome["event_intraday_return"] == pytest.approx(22.29 / 22 - 1)
    assert outcome["event_close_return"] == pytest.approx(22.29 / float(bars[event_index - 1].close) - 1)
    assert outcome["baseline_avg_volume"] == 100
    assert outcome["event_volume_ratio"] == 3
    assert outcome["bearish_mfe_5d"] == pytest.approx((22.29 - min(b.low for b in bars[26:31])) / 22.29)
    assert outcome["bearish_mae_5d"] == pytest.approx((max(b.high for b in bars[26:31]) - 22.29) / 22.29)
    assert outcome["event_status"] == "post_event_incomplete"
    assert outcome["max_post_event_session_available"] == 35


def test_upcoming_and_event_today_status_are_explicit():
    bars = [bar(date(2026, 4, 6), 21.63), bar(date(2026, 4, 7), 22.29)]
    upcoming = compute_event_outcome(bars[:1], date(2026, 4, 7), None)
    today = compute_event_outcome(bars, date(2026, 4, 7), date(2026, 4, 7))
    assert upcoming["event_status"] == "upcoming"
    assert upcoming["max_post_event_session_available"] is None
    assert today["event_status"] == "event_today"
    assert today["max_post_event_session_available"] == 0
