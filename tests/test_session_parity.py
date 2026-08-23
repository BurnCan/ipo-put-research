"""Offline regression tests for the read-only M6 canonical-session audit."""
from datetime import date

from sqlalchemy import inspect, select

from app.models import (LockupEventAnalysis, LockupProspectiveSignal,
                        LockupSignalSnapshot)
from app.services.event_analysis.analysis import recompute_lockup_analysis
from app.services.event_analysis.session_parity import (audit_m6_session_parity,
                                                         mismatching_session_parity_rows,
                                                         summarize_session_parity)
from app.services.market_calendar import session_offset
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
    assert row.mismatch_type == "observation_session_mismatch_sparse_history"
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
    assert report["sparse_market_history_cases"] == sum(
        r.mismatch_type == "observation_session_mismatch_sparse_history" for r in rows)
    assert report["unexplained_mismatches"] == sum(
        r.mismatch_type == "observation_session_mismatch_unexplained" for r in rows)


def _canonical_sessions_ending(day, count):
    dates = [day]
    for _ in range(count - 1):
        dates.append(session_offset(dates[-1], -1))
    return list(reversed(dates))


def _snapshot(db, offset=-5):
    return db.scalar(select(LockupSignalSnapshot).where(
        LockupSignalSnapshot.observation_offset == offset))


def test_each_row_level_mismatch_class_and_mismatch_selection():
    event = date(2026, 7, 29)
    observation = session_offset(event, -5)
    dates = _canonical_sessions_ending(event, 30)

    # Event-only mismatch.
    db, lockup, security = analysis_database(event, dates)
    recompute_lockup_analysis(db, lockup, security)
    snapshot = _snapshot(db)
    snapshot.event_trade_date = session_offset(event, -1)
    db.commit()
    row = next(r for r in _audit(db) if r.observation_offset == -5)
    assert (row.event_session_match, row.observation_session_match) == (False, True)
    assert row.mismatch_type == "event_session_mismatch"
    db.close()

    # Observation-only mismatch not explained by sparse history.
    db, lockup, security = analysis_database(event, dates)
    recompute_lockup_analysis(db, lockup, security)
    snapshot = _snapshot(db)
    snapshot.observation_date = session_offset(observation, -1)
    db.commit()
    rows = _audit(db)
    row = next(r for r in rows if r.observation_offset == -5)
    assert (row.event_session_match, row.observation_session_match,
            row.old_bar_offset_reproduced) == (True, False, False)
    assert row.mismatch_type == "observation_session_mismatch_unexplained"
    assert mismatching_session_parity_rows(rows) == [row]

    # Both identities disagree.
    snapshot.event_trade_date = session_offset(event, -1)
    db.commit()
    row = next(r for r in _audit(db) if r.observation_offset == -5)
    assert row.mismatch_type == "event_and_observation_mismatch"
    db.close()


def test_missing_required_fields_ticker_lockup_filters_and_ordering():
    event = date(2026, 7, 29)
    db, lockup, security = analysis_database(event, _canonical_sessions_ending(event, 30))
    recompute_lockup_analysis(db, lockup, security)
    lockup.stated_expiration_date = None
    lockup.calculated_expiration_date = None
    db.commit()
    rows = _audit(db)
    assert rows
    assert all(row.mismatch_type == "missing_required_fields" for row in rows)
    assert audit_m6_session_parity(
        db, classification_status=None, candidate_type=None, offering_status=None,
        primary_lockup_only=False, ticker="ses") == rows
    assert audit_m6_session_parity(
        db, classification_status=None, candidate_type=None, offering_status=None,
        primary_lockup_only=False, ticker="NOPE") == []
    assert audit_m6_session_parity(
        db, classification_status=None, candidate_type=None, offering_status=None,
        primary_lockup_only=False, lockup_id=lockup.id) == rows
    assert audit_m6_session_parity(
        db, classification_status=None, candidate_type=None, offering_status=None,
        primary_lockup_only=False, lockup_id=lockup.id + 999) == []
    assert rows == sorted(rows, key=lambda r: ((r.ticker or ""), r.lockup_id,
                                               r.observation_offset, r.snapshot_id))
    assert [r.observation_offset for r in rows] == [-5, -1]
    db.close()


def _discovery_report(*, omit_required_session=False):
    event = date(2026, 7, 29)
    observation = session_offset(event, -5)
    required = _canonical_sessions_ending(observation, 21)
    dates = required + _canonical_sessions_ending(event, 5)[1:]
    if omit_required_session:
        dates.remove(required[7])
        dates.insert(0, session_offset(required[0], -1))
    db, lockup, security = analysis_database(event, dates)
    recompute_lockup_analysis(db, lockup, security)
    snapshot = _snapshot(db)
    assert snapshot.return_20d is not None and snapshot.realized_vol_20d is not None
    snapshot.observation_date = session_offset(observation, -1)
    outcome = db.scalar(select(LockupEventAnalysis))
    outcome.post_20d_return = -.1
    db.commit()
    return db, summarize_session_parity(db, _audit(db))


def test_m7_discovery_impact_and_exact_canonical_feature_coverage():
    db, report = _discovery_report()
    assert report["m7_discovery_events"] == 1
    assert report["m7_events_with_session_mismatch"] == 1
    assert report["m7_events_with_exact_session_match"] == 0
    assert report["canonical_features_recomputable"] == 1
    assert report["canonical_features_not_recomputable_due_to_missing_bars"] == 0
    db.close()


def test_m7_sparse_21_bar_history_is_not_canonically_recomputable():
    db, report = _discovery_report(omit_required_session=True)
    assert report["m7_discovery_events"] == 1
    assert report["canonical_features_recomputable"] == 0
    assert report["canonical_features_not_recomputable_due_to_missing_bars"] == 1
    db.close()


def _state(row):
    return tuple(getattr(row, column.key) for column in inspect(row).mapper.column_attrs)


def test_audit_mutates_no_m6_m7_or_m8_rows():
    event = date(2026, 7, 29)
    db, lockup, security = analysis_database(event, _canonical_sessions_ending(event, 30))
    recompute_lockup_analysis(db, lockup, security)
    snapshot = _snapshot(db)
    outcome = db.scalar(select(LockupEventAnalysis))
    signal = LockupProspectiveSignal(
        hypothesis_id="test", hypothesis_version="test", ipo_id=snapshot.ipo_id,
        lockup_id=lockup.id, security_id=security.id, observation_offset=-5,
        event_date=event, feature1_name="return_20d", feature1_threshold=.1,
        feature2_name="realized_vol_20d", feature2_threshold=.2,
        signal_status="pending")
    db.add(signal)
    db.commit()
    before = [_state(row) for row in (snapshot, outcome, signal)]
    summarize_session_parity(db, _audit(db))
    after = [_state(row) for row in (snapshot, outcome, signal)]
    assert after == before
    assert not db.new and not db.dirty and not db.deleted
    db.close()
