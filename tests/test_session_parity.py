"""Offline regression tests for the read-only M6 canonical-session audit."""
from datetime import date

from sqlalchemy import select

from app.models import LockupSignalSnapshot
from app.services.event_analysis.analysis import recompute_lockup_analysis
from app.services.event_analysis.session_parity import (audit_m6_session_parity,
                                                         summarize_session_parity)
from tests.test_event_analysis import analysis_database


def _audit(db):
    return audit_m6_session_parity(
        db, classification_status=None, candidate_type=None, offering_status=None,
        primary_lockup_only=False)


def test_sparse_stored_offset_is_reproduced_and_missing_sessions_are_explicit():
    event = date(2026, 7, 29)
    db, lockup, security = analysis_database(event, [
        date(2026, 7, 13), date(2026, 7, 14), date(2026, 7, 15),
        date(2026, 7, 16), date(2026, 7, 24), event])
    recompute_lockup_analysis(db, lockup, security)
    row = next(r for r in _audit(db) if r.observation_offset == -5)
    assert row.event_session_match is True
    assert row.observation_session_match is False
    assert row.canonical_observation_date == date(2026, 7, 22)
    assert row.old_bar_offset_reproduced is True
    assert row.mismatch_type == "observation_session_mismatch"
    assert row.expected_session_count == 6
    assert row.stored_sessions_in_expected_window == (date(2026, 7, 24), event)
    assert row.missing_expected_session_count == 4


def test_exact_match_multiple_offsets_and_stated_date_precedence():
    event = date(2026, 7, 29)
    dates = [date(2026, 7, 21), date(2026, 7, 22), date(2026, 7, 23),
             date(2026, 7, 24), date(2026, 7, 27), date(2026, 7, 28), event]
    db, lockup, security = analysis_database(event, dates)
    lockup.calculated_expiration_date = date(2026, 8, 3)
    recompute_lockup_analysis(db, lockup, security)
    rows = _audit(db)
    assert [r.observation_offset for r in rows] == [-5, -1]
    assert all(r.mismatch_type == "exact_match" for r in rows)
    assert all((r.event_date, r.event_date_source) == (event, "stated") for r in rows)


def test_audit_is_deterministic_read_only_and_summary_has_metadata():
    event = date(2026, 6, 1)
    db, lockup, security = analysis_database(event, [
        date(2026, 5, 4), date(2026, 5, 5), date(2026, 5, 6),
        date(2026, 5, 22), date(2026, 5, 29), event])
    lockup.calculated_expiration_date = event
    lockup.stated_expiration_date = None
    recompute_lockup_analysis(db, lockup, security)
    before = [(r.id, r.observation_date, r.event_trade_date) for r in
              db.scalars(select(LockupSignalSnapshot).order_by(LockupSignalSnapshot.id))]
    rows = _audit(db)
    report = summarize_session_parity(db, rows)
    after = [(r.id, r.observation_date, r.event_trade_date) for r in
             db.scalars(select(LockupSignalSnapshot).order_by(LockupSignalSnapshot.id))]
    assert before == after
    assert not db.new and not db.dirty and not db.deleted
    assert rows == _audit(db)
    assert all(r.event_date_source == "calculated" for r in rows)
    assert report["canonical_calendar_id"] == "XNYS"
    assert report["canonical_calendar_provider"] == "exchange_calendars"
    assert report["canonical_calendar_version"]
