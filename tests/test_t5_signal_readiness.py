"""Focused tests for the read-only T-5 signal-readiness audit."""
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import (Company, DailyPrice, Filing, IPO, IPOLockup,
                        MarketDataBackfillAttempt, Security)
from app.services.market_calendar import resolve_observation_session, session_offset
from app.services.market_data.t5_readiness import audit_t5_signal_readiness
from scripts.audit_t5_signal_readiness import json_default


def _db(event_day=date(2026, 6, 15)):
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db = Session(engine)
    company = Company(cik="0000000123", name="Audit Co", ticker="AUDT")
    filing = Filing(company=company, form_type="424B4", filed_at=date(2025, 1, 1),
                    accession_number="x", filing_path="x", sec_url="x")
    ipo = IPO(company=company, classification_status="classified",
              candidate_type="operating_company_ipo", offering_status="priced")
    security = Security(company=company, ticker="AUDT", source="test")
    lockup = IPOLockup(ipo=ipo, filing=filing, holder_group="all", lockup_type="days",
                       stated_expiration_date=event_day, confidence=1,
                       parser_name="test", parser_version="1", source_excerpt="x",
                       source_locator="x", evidence_key="unique")
    db.add_all([company, filing, ipo, security, lockup]); db.flush()
    ipo.primary_lockup_id = lockup.id; db.flush()
    observation = resolve_observation_session(event_day, -5).observation_session
    required = tuple(session_offset(observation, offset) for offset in range(-20, 1))
    return db, engine, security, lockup, required


def _bar(db, security, day):
    db.add(DailyPrice(security_id=security.id, trade_date=day, provider="other",
                      provider_symbol=security.ticker, open=1, high=1, low=1,
                      close=1, volume=1))


def _attempt(db, security, day, status, hour=0):
    db.add(MarketDataBackfillAttempt(
        security_id=security.id, provider="configured", requested_start_date=day,
        requested_end_date=day, status=status,
        attempted_at=datetime(2026, 7, 1, hour, tzinfo=timezone.utc)))


def _report(db, as_of=date(2026, 7, 1)):
    db.flush()
    return audit_t5_signal_readiness(db, provider="configured", as_of_date=as_of)


def test_complete_exact_21_session_window_and_provider_independent_bars():
    db, _, security, _, required = _db()
    for day in required: _bar(db, security, day)
    _attempt(db, security, required[0], "error")
    item = _report(db)["details"][0]
    assert item["readiness"] == "complete"
    assert item["required_sessions"] == list(required)
    assert item["required_session_count"] == item["present_session_count"] == 21


def test_missing_sessions_do_not_compress_window_and_all_no_data_is_exhausted():
    db, _, security, _, required = _db()
    for day in required[9:]: _bar(db, security, day)
    for day in required[:9]: _attempt(db, security, day, "no_data")
    item = _report(db)["details"][0]
    assert item["readiness"] == "provider_exhausted"
    assert item["required_session_count"] == 21
    assert item["present_session_count"] == 12
    assert item["known_no_data_dates"] == list(required[:9])


def test_error_precedes_no_data_and_unattempted_readiness():
    db, _, security, _, required = _db()
    _attempt(db, security, required[0], "no_data")
    _attempt(db, security, required[1], "error")
    item = _report(db)["details"][0]
    assert item["readiness"] == "provider_error"
    assert item["provider_error_dates"] == [required[1]]

    db2, _, security2, _, required2 = _db()
    _attempt(db2, security2, required2[0], "no_data")
    item2 = _report(db2)["details"][0]
    assert item2["readiness"] == "backfill_candidate"
    assert required2[1] in item2["unattempted_retryable_dates"]


def test_later_no_data_resolves_historical_error():
    db, _, security, _, required = _db()
    for day in required:
        _attempt(db, security, day, "error", hour=1)
        _attempt(db, security, day, "no_data", hour=2)
    item = _report(db)["details"][0]
    assert item["readiness"] == "provider_exhausted"
    assert item["provider_error_count"] == 0


def test_actual_bar_wins_over_attempt_history():
    db, _, security, _, required = _db()
    for day in required: _attempt(db, security, day, "error")
    _bar(db, security, required[0])
    item = _report(db)["details"][0]
    assert required[0] in item["present_sessions"]
    assert required[0] not in item["provider_error_dates"]


def test_future_t5_is_not_reached_and_future_is_not_retryable():
    event_day = date.today() + timedelta(days=60)
    db, _, _, _, required = _db(event_day)
    item = _report(db, as_of=date.today())["details"][0]
    assert item["readiness"] == "not_reached"
    assert item["future_not_reached_dates"]
    assert not item["unattempted_retryable_dates"]
    assert required[-1] == item["t5_observation_session"]


def test_read_only_and_json_date_serialization():
    db, engine, _, _, _ = _db()
    writes = []
    def observe(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            writes.append(statement)
    event.listen(engine, "before_cursor_execute", observe)
    _report(db)
    assert writes == []
    assert json_default(date(2026, 8, 29)) == "2026-08-29"
