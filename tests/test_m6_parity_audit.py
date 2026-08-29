"""Offline tests for the persisted M6 v1/v2 audit."""
import json
from datetime import date

from sqlalchemy import select

from app.models import LockupSignalSnapshot
from app.services.event_analysis.analysis import recompute_lockup_analysis
from app.services.event_analysis.m6_parity_audit import (
    AUDITED_FEATURES, _pair, audit_m6_v1_v2, numeric_match, summarize_m6_parity)
from app.services.market_calendar import session_offset, sessions_in_range
from tests.test_event_analysis import analysis_database


def _database():
    event = date(2026, 6, 15)
    dates = sessions_in_range(session_offset(event, -80), session_offset(event, 40))
    db, lockup, security = analysis_database(event, dates)
    recompute_lockup_analysis(db, lockup, security, snapshot_version="1")
    recompute_lockup_analysis(db, lockup, security, snapshot_version="2")
    return db, lockup, security


def _rows(db, offset=-5):
    rows = list(db.scalars(select(LockupSignalSnapshot).where(
        LockupSignalSnapshot.observation_offset == offset).order_by(
            LockupSignalSnapshot.snapshot_version)))
    return rows[0], rows[1]


def _audit(db, **kwargs):
    return audit_m6_v1_v2(
        db, classification_status=None, candidate_type=None, offering_status=None,
        primary_lockup_only=False, **kwargs)


def test_numeric_null_and_tolerance_rules():
    assert numeric_match(None, None)
    assert not numeric_match(None, 1)
    assert not numeric_match(1, None)
    assert numeric_match(100, 100.000009, atol=1e-9, rtol=1e-7)
    assert not numeric_match(100, 100.00002, atol=1e-9, rtol=1e-7)


def test_exact_pair_identity_and_features():
    db, _, _ = _database()
    try:
        v1, v2 = _rows(db)
        pair = _pair(v1, v2, "TEST", atol=1e-9, rtol=1e-7)
        assert pair.classification == "exact_match"
        assert pair.exact_identity_match
        assert all(pair.feature_matches.values())
    finally:
        db.close()


def test_observation_and_complete_numeric_mismatch_classifications():
    db, _, _ = _database()
    try:
        v1, v2 = _rows(db)
        v2.observation_date = session_offset(v2.observation_date, -1)
        assert _pair(v1, v2, "TEST", atol=1e-9, rtol=1e-7).classification == "observation_session_mismatch"
        v2.observation_date = v1.observation_date
        v2.close = float(v1.close) + 1
        assert _pair(v1, v2, "TEST", atol=1e-9, rtol=1e-7).classification == "numeric_feature_mismatch_complete_history"
    finally:
        db.close()


def test_sparse_history_null_feature_is_explained():
    db, _, _ = _database()
    try:
        v1, v2 = _rows(db)
        assert v1.return_20d is not None
        v2.return_20d = None
        v2.snapshot_status = "partial"
        v2.unavailable_reason = "missing_feature_history"
        pair = _pair(v1, v2, "TEST", atol=1e-9, rtol=1e-7)
        assert pair.classification == "sparse_history_feature_mismatch"
        assert not pair.feature_matches["return_20d"]
    finally:
        db.close()


def test_unavailable_reasons_and_status_summary():
    db, _, _ = _database()
    try:
        v1, v2 = _rows(db)
        v2.snapshot_status = "unavailable"
        v2.unavailable_reason = "observation_not_reached"
        future = _pair(v1, v2, "TEST", atol=1e-9, rtol=1e-7)
        assert future.classification == "v2_observation_not_reached"
        v2.unavailable_reason = "missing_observation_bar"
        missing = _pair(v1, v2, "TEST", atol=1e-9, rtol=1e-7)
        assert missing.classification == "v2_missing_observation_bar"
        v2.snapshot_status = "partial"; v2.unavailable_reason = "missing_feature_history"
        partial = _pair(v1, v2, "TEST", atol=1e-9, rtol=1e-7)
        report = summarize_m6_parity(db, [future, missing, partial])
        assert report["v2_unavailable"] == 2
        assert report["v2_partial"] == 1
        assert report["v2_completeness"]["by_unavailable_reason"]["missing_observation_bar"] == 1
    finally:
        db.close()


def test_missing_versions_empty_cohort_json_and_read_only_idempotence():
    db, lockup, _ = _database()
    try:
        before = [(r.id, r.snapshot_version, r.close) for r in db.scalars(
            select(LockupSignalSnapshot).order_by(LockupSignalSnapshot.id))]
        first = _audit(db)
        second = _audit(db)
        after = [(r.id, r.snapshot_version, r.close) for r in db.scalars(
            select(LockupSignalSnapshot).order_by(LockupSignalSnapshot.id))]
        assert first == second
        assert before == after
        assert not db.new and not db.dirty and not db.deleted
        assert json.loads(json.dumps(first, default=lambda x: x.isoformat()))["read_only"] is True
        empty = _audit(db, lockup_id=lockup.id + 999)
        assert empty["rows_seen"] == empty["comparable_pairs"] == 0
        assert empty["m7_frozen_hypothesis"]["m7_rows_seen"] == 0

        # Removing one lineage produces stable union-pair accounting without writes.
        v1, v2 = _rows(db)
        db.expunge(v2)
        db.rollback()  # no persisted change; direct pairing exercises missing v2.
        no_v2 = _pair(v1, None, "TEST", atol=1e-9, rtol=1e-7)
        no_v1 = _pair(None, v2, "TEST", atol=1e-9, rtol=1e-7)
        missing = summarize_m6_parity(db, [no_v1, no_v2])
        assert missing["no_v1_snapshot"] == missing["no_v2_snapshot"] == 1
    finally:
        db.close()


def test_feature_report_covers_every_persisted_audited_feature_and_caps_examples():
    db, _, _ = _database()
    try:
        v1, v2 = _rows(db)
        v2.close = float(v1.close) + 1
        pair = _pair(v1, v2, "TEST", atol=1e-9, rtol=1e-7)
        report = summarize_m6_parity(db, [pair], max_examples=0)
        assert set(report["feature_parity"]) == set(AUDITED_FEATURES)
        assert report["feature_parity"]["close"]["mismatched"] == 1
        assert report["mismatch_examples"] == []
        assert report["unexplained_complete_history_mismatches"] == 1
    finally:
        db.close()


def test_m7_hypothetical_comparison_is_read_only_and_reports_crossing_and_sparse():
    db, _, _ = _database()
    try:
        v1, v2 = _rows(db)
        exact = _pair(v1, v2, "TEST", atol=1e-9, rtol=1e-7)
        exact_report = summarize_m6_parity(db, [exact])["m7_frozen_hypothesis"]
        assert exact_report["m7_rows_seen"] == 1
        assert exact_report["features"]["return_20d"]["matches_v1"] == 1

        # This is an in-memory hypothetical fixture; the audit performs no assignment.
        v2.return_20d = -1 if float(v1.return_20d) > .0332778702 else 1
        crossing = _pair(v1, v2, "TEST", atol=1e-9, rtol=1e-7)
        crossing_report = summarize_m6_parity(db, [crossing])["m7_frozen_hypothesis"]
        assert crossing_report["features"]["return_20d"]["threshold_crossings"] == 1
        assert crossing_report["features"]["return_20d"]["differs_complete_canonical_data"] == 1

        v2.return_20d = None
        v2.snapshot_status = "partial"
        v2.unavailable_reason = "missing_feature_history"
        sparse = _pair(v1, v2, "TEST", atol=1e-9, rtol=1e-7)
        sparse_report = summarize_m6_parity(db, [sparse])["m7_frozen_hypothesis"]
        assert sparse_report["features"]["return_20d"]["differs_missing_canonical_data"] == 1
    finally:
        db.rollback()
        db.close()
