"""Offline checks for the read-only research dashboard projection."""
import ast
from datetime import date, timedelta
import inspect
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import (Company, Filing, IPO, IPOLockup, LockupProspectiveSignal,
                        LockupSignalSnapshot, Security)
from app.services.backtest.analysis import FROZEN_HYPOTHESES
from app.services.event_analysis.constants import SNAPSHOT_VERSION
from app.services.research_dashboard import (HYPOTHESIS_ID, classify_prospective_result,
                                               get_research_summary,
                                               get_upcoming_lockups,
                                               hypothesis_metadata)
from app.services import research_dashboard


CUTOFF = date(2026, 8, 23)


def _add_lockup(db, number, observation_date=None, signal_status=None,
                unavailable_reason=None):
    ticker = f"T{number}"
    company = Company(cik=f"{number:010d}", name=f"Company {number}", ticker=ticker)
    filing = Filing(company=company, form_type="424B4", filed_at=date(2026, 1, 2),
                    accession_number=f"filing-{number}", filing_path="filing.txt",
                    sec_url="https://example.test/filing")
    ipo = IPO(company=company, ipo_date=date(2026, 1, 2), ipo_price=10,
              classification_status="classified", candidate_type="operating_company_ipo",
              offering_status="priced")
    event_date = (observation_date or CUTOFF) + timedelta(days=7)
    lockup = IPOLockup(ipo=ipo, filing=filing, holder_group="all", lockup_type="standard",
                       stated_expiration_date=event_date, confidence=.9, parser_name="test",
                       parser_version="1", source_excerpt="test", source_locator="test",
                       evidence_key=f"lockup-{number}")
    security = Security(company=company, ticker=ticker, source="test")
    db.add_all((lockup, security))
    db.flush()
    ipo.primary_lockup_id = lockup.id
    ipo.primary_lockup_expiration_date = event_date
    if observation_date is not None:
        db.add(LockupSignalSnapshot(
            ipo_id=ipo.id, lockup_id=lockup.id, security_id=security.id,
            observation_offset=-5, observation_date=observation_date,
            data_cutoff_date=observation_date, event_date=event_date,
            event_date_source="stated", event_trade_date=event_date,
            snapshot_version=SNAPSHOT_VERSION, trading_sessions_to_event=5,
            trading_sessions_since_first_trade=50, available_history_sessions=50,
            close=10, return_20d=.04, realized_vol_20d=.9,
            post_ipo_high_to_date=12, post_ipo_low_to_date=8))
    if signal_status is not None:
        spec = FROZEN_HYPOTHESES[HYPOTHESIS_ID]
        db.add(LockupProspectiveSignal(
            hypothesis_id=HYPOTHESIS_ID, hypothesis_version=spec.analysis_version,
            ipo_id=ipo.id, lockup_id=lockup.id, security_id=security.id,
            observation_offset=-5, observation_date=observation_date,
            event_date=event_date, event_trade_date=event_date,
            feature1_name=spec.feature1, feature1_value=.04,
            feature1_threshold=spec.feature1_threshold, feature1_side="low",
            feature2_name=spec.feature2, feature2_value=.9,
            feature2_threshold=spec.feature2_threshold, feature2_side="high",
            interaction_group="low_high", is_high_high=False,
            signal_status=signal_status,
            unavailable_reason=unavailable_reason,
            evaluation_mode=("lifecycle_tracking" if signal_status == "unavailable"
                             else "prospective")))
    db.flush()
    return lockup.id


def _dashboard_database():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = Session(engine)
    historical_id = _add_lockup(db, 1, CUTOFF)
    pending_id = _add_lockup(db, 2, CUTOFF + timedelta(days=1))
    signaled_id = _add_lockup(db, 3, CUTOFF - timedelta(days=1), "awaiting_event")
    db.commit()
    return db, historical_id, pending_id, signaled_id


def _add_signal_mode(db, lockup_id, mode, *, feature1=.2209, feature2=.5146,
                     group="high_low", status="awaiting_event"):
    """Add a coexisting stored signal using the lockup fixture identities."""
    source = db.scalar(select(LockupProspectiveSignal).where(
        LockupProspectiveSignal.lockup_id == lockup_id))
    db.add(LockupProspectiveSignal(
        hypothesis_id=source.hypothesis_id, hypothesis_version=source.hypothesis_version,
        ipo_id=source.ipo_id, lockup_id=source.lockup_id, security_id=source.security_id,
        observation_offset=source.observation_offset, observation_date=source.observation_date,
        required_observation_date=source.observation_date, calendar_id="XNYS",
        calendar_provider="exchange_calendars", calendar_version="test",
        event_date=source.event_date, event_trade_date=source.event_trade_date,
        feature1_name=source.feature1_name, feature1_value=feature1,
        feature1_threshold=source.feature1_threshold, feature1_side="high",
        feature2_name=source.feature2_name, feature2_value=feature2,
        feature2_threshold=source.feature2_threshold, feature2_side="low",
        interaction_group=group, is_high_high=False, signal_status=status,
        evaluation_mode=mode))
    db.flush()


def test_hypothesis_metadata_is_registry_projection():
    spec = FROZEN_HYPOTHESES[HYPOTHESIS_ID]
    payload = hypothesis_metadata()

    assert payload["feature1"]["threshold"] is spec.feature1_threshold
    assert payload["feature2"]["threshold"] is spec.feature2_threshold
    assert payload["prospective_start_date"] == date(2026, 8, 23)
    assert payload["observation_offset"] == spec.observation_offset


def test_root_is_research_dashboard_without_legacy_actions_or_raw_row_navigation():
    page = Path("app/templates/index.html").read_text(encoding="utf-8")

    assert "Lockup Expiration Research Dashboard" in page
    assert "FROZEN HYPOTHESIS" in page
    assert "PROSPECTIVE · OUT-OF-SAMPLE" in page
    assert "NOT OUT-OF-SAMPLE" in page
    assert "Ingest last 365 days" not in page
    assert "window.location='/api/ipos/" not in page
    assert "v??'—'" in page  # the escaping helper has an explicit null fallback


def test_research_routes_are_get_only():
    routes = Path("app/api/routes.py").read_text(encoding="utf-8")
    required_get_routes = {
        "hypothesis", "summary", "upcoming-lockups", "prospective-signals",
        "prospective-evaluation", "shadow-evaluation", "historical-reference",
    }
    for route in required_get_routes:
        assert f'@router.get("/research/{route}")' in routes
    for method in ("post", "put", "patch", "delete"):
        assert f'@router.{method}("/research/' not in routes


def test_upcoming_projection_is_prospective_only_and_stored_signal_wins():
    db, historical_id, pending_id, signaled_id = _dashboard_database()
    try:
        rows = {row["lockup_id"]: row for row in get_upcoming_lockups(db)}

        assert historical_id not in rows
        assert rows[pending_id]["m8_status"] == "pending_observation"
        assert rows[pending_id]["t5_timing_status"] == "t5_snapshot_available"
        assert rows[pending_id]["t5_observation_date"] == date(2026, 8, 24)
        assert rows[signaled_id]["m8_status"] == "awaiting_event"
        assert rows[signaled_id]["t5_timing_status"] == "signal_frozen"
        assert rows[signaled_id]["minus5_observation_date"] == date(2026, 8, 22)
        assert all(row["m8_status"] != "unavailable_historical" for row in rows.values())
    finally:
        db.close()


def test_upcoming_rows_are_sorted_and_required_t5_is_calendar_derived():
    db, historical_id, pending_id, signaled_id = _dashboard_database()
    try:
        no_snapshot_id = _add_lockup(db, 4)
        db.commit()
        rows = get_upcoming_lockups(db, today=CUTOFF)
        assert [row["lockup_event_date"] for row in rows] == sorted(
            row["lockup_event_date"] for row in rows)
        row = next(row for row in rows if row["lockup_id"] == no_snapshot_id)
        assert row["required_t5_date"] == date(2026, 8, 24)
        assert row["stored_t5_snapshot_date"] is None
        assert row["calendar_id"] == "XNYS"
        assert row["t5_snapshot_available"] is False
        assert row["t5_timing_status"] == "waiting_for_t5"
        assert row["m8_status"] == "pending_observation"
    finally:
        db.close()


def test_stored_prospective_signal_t5_date_is_authoritative():
    db, historical_id, pending_id, signaled_id = _dashboard_database()
    try:
        signal = db.scalar(select(LockupProspectiveSignal).where(
            LockupProspectiveSignal.lockup_id == signaled_id))
        signal.observation_date = CUTOFF + timedelta(days=2)
        db.commit()
        row = next(row for row in get_upcoming_lockups(db)
                   if row["lockup_id"] == signaled_id)
        assert row["minus5_observation_date"] == CUTOFF - timedelta(days=1)
        assert row["required_t5_date"] == CUTOFF + timedelta(days=2)
        assert row["signal_observation_date"] == CUTOFF + timedelta(days=2)
        assert row["t5_observation_date"] == CUTOFF + timedelta(days=2)
    finally:
        db.close()


def test_stored_prospective_required_t5_date_takes_precedence():
    db, _historical_id, _pending_id, signaled_id = _dashboard_database()
    try:
        signal = db.scalar(select(LockupProspectiveSignal).where(
            LockupProspectiveSignal.lockup_id == signaled_id))
        signal.observation_date = CUTOFF + timedelta(days=2)
        signal.required_observation_date = CUTOFF + timedelta(days=3)
        db.commit()

        row = next(row for row in get_upcoming_lockups(db)
                   if row["lockup_id"] == signaled_id)
        assert row["required_t5_date"] == CUTOFF + timedelta(days=3)
        assert row["signal_observation_date"] == CUTOFF + timedelta(days=2)
        assert row["stored_t5_snapshot_date"] == CUTOFF - timedelta(days=1)
        assert row["t5_observation_date"] == CUTOFF + timedelta(days=3)
    finally:
        db.close()


def test_lifecycle_unavailable_uses_stored_required_t5_date():
    db, _historical_id, _pending_id, _signaled_id = _dashboard_database()
    try:
        missed_id = _add_lockup(db, 7, CUTOFF, "unavailable",
                                "observation_before_prospective_start")
        signal = db.scalar(select(LockupProspectiveSignal).where(
            LockupProspectiveSignal.lockup_id == missed_id))
        signal.required_observation_date = CUTOFF - timedelta(days=3)
        db.commit()

        row = next(row for row in get_upcoming_lockups(db)
                   if row["lockup_id"] == missed_id)
        assert row["required_t5_date"] == CUTOFF - timedelta(days=3)
        assert row["signal_observation_date"] == CUTOFF
        assert row["stored_t5_snapshot_date"] == CUTOFF
    finally:
        db.close()


def test_shadow_signal_wins_over_lifecycle_and_supplies_frozen_features_without_snapshot():
    db, _historical_id, _pending_id, _signaled_id = _dashboard_database()
    try:
        lockup_id = _add_lockup(db, 8, CUTOFF, "unavailable",
                                "observation_before_prospective_start")
        snapshot = db.scalar(select(LockupSignalSnapshot).where(
            LockupSignalSnapshot.lockup_id == lockup_id))
        db.delete(snapshot)
        _add_signal_mode(db, lockup_id, "shadow_prospective")
        db.commit()

        row = next(row for row in get_upcoming_lockups(db)
                   if row["lockup_id"] == lockup_id)
        assert row["m8_status"] == "awaiting_event"
        assert row["evaluation_mode"] == "shadow_prospective"
        assert row["prospective_track"] == "shadow"
        assert row["t5_timing_status"] == "shadow_signal_frozen"
        assert row["return_20d_at_minus5"] == .2209
        assert row["realized_vol_20d_at_minus5"] == .5146
        assert row["interaction_group"] == "high_low"
        assert row["is_high_high"] is False
        assert row["t5_snapshot_available"] is False
        assert row["signal_locked_at"] is not None
    finally:
        db.close()


def test_strict_signal_wins_when_all_signal_modes_coexist():
    db, _historical_id, _pending_id, _signaled_id = _dashboard_database()
    try:
        lockup_id = _add_lockup(db, 9, CUTOFF, "unavailable",
                                "observation_before_prospective_start")
        _add_signal_mode(db, lockup_id, "shadow_prospective", feature1=.22)
        _add_signal_mode(db, lockup_id, "strict_prospective", feature1=.33,
                         group="high_high", status="awaiting_outcome")
        db.commit()

        row = next(row for row in get_upcoming_lockups(db)
                   if row["lockup_id"] == lockup_id)
        assert row["evaluation_mode"] == "strict_prospective"
        assert row["prospective_track"] == "strict"
        assert row["m8_status"] == "awaiting_outcome"
        assert row["return_20d_at_minus5"] == .33
        assert row["interaction_group"] == "high_high"
    finally:
        db.close()


def test_summary_keeps_strict_and_shadow_counts_separate_with_legacy_as_strict():
    db, _historical_id, _pending_id, signaled_id = _dashboard_database()
    try:
        _add_signal_mode(db, signaled_id, "shadow_prospective")
        shadow_only_id = _add_lockup(db, 10, CUTOFF, "unavailable",
                                     "observation_before_prospective_start")
        _add_signal_mode(db, shadow_only_id, "shadow_prospective")
        db.commit()

        summary = get_research_summary(db)
        assert summary["prospective_signals"] == 1  # legacy ``prospective`` only
        assert summary["shadow_signals"] == 2
        assert summary["awaiting_event"] == 1
    finally:
        db.close()


def test_no_stored_signal_is_not_projected_as_shadow():
    db, _historical_id, _pending_id, _signaled_id = _dashboard_database()
    try:
        lockup_id = _add_lockup(db, 11)
        db.commit()
        row = next(row for row in get_upcoming_lockups(db, today=CUTOFF)
                   if row["lockup_id"] == lockup_id)
        assert row["evaluation_mode"] is None
        assert row["prospective_track"] is None
        assert row["t5_timing_status"] != "shadow_signal_frozen"
    finally:
        db.close()


def test_upcoming_template_has_collapsed_accessible_control_and_preserved_labels():
    page = Path("app/templates/index.html").read_text(encoding="utf-8")
    assert "UPCOMING_INITIAL_LIMIT=10" in page
    assert 'aria-expanded="false"' in page
    assert "Show fewer" in page and "Show all ${xs.length}" in page
    assert "prospective-side ${xs.length===1?'event':'events'}" in page
    assert "HISTORICAL DISCOVERY SAMPLE · NOT OUT-OF-SAMPLE" in page
    assert "No prospective M8 signals have been frozen yet." in page
    assert "No prospective outcomes have matured yet." in page
    assert "Not eligible prospectively" in page
    assert "T-5 observation predates hypothesis freeze" in page
    for label in ("Pre-event 20d return", "Pre-event 20d realized vol",
                  "Outcome status", "Post-event 20d return", "Result"):
        assert label in page
        assert f'data-label="{label}"' in page


def test_result_classification_is_mature_only_target_aware_and_track_agnostic():
    class Signal:
        signal_status = "matured"
        interaction_group = "high_high"
        realized_outcome_value = -.01

    signal = Signal()
    assert classify_prospective_result(signal) == "bearish_hit"
    signal.realized_outcome_value = .01
    assert classify_prospective_result(signal) == "no_bearish_hit"
    for group in ("high_low", "low_high", "low_low"):
        signal.interaction_group = group
        signal.realized_outcome_value = -.01
        assert classify_prospective_result(signal) == "bearish_non_target"
        signal.realized_outcome_value = .01
        assert classify_prospective_result(signal) == "non_target"
    signal.signal_status = "awaiting_outcome"
    signal.realized_outcome_value = -.50
    assert classify_prospective_result(signal) is None


def test_upcoming_projects_only_stored_mature_outcome_and_provenance():
    db, _historical_id, _pending_id, signaled_id = _dashboard_database()
    try:
        signal = db.scalar(select(LockupProspectiveSignal).where(
            LockupProspectiveSignal.lockup_id == signaled_id))
        signal.signal_status = "awaiting_outcome"
        signal.realized_outcome_value = -.99  # defensive: partial/stale is hidden
        db.commit()
        row = next(r for r in get_upcoming_lockups(db) if r["lockup_id"] == signaled_id)
        assert row["outcome_status"] == "awaiting_outcome"
        assert row["post_event_20d_return"] is None
        assert row["result"] is None

        signal.signal_status = "matured"
        signal.interaction_group = "high_high"
        signal.is_high_high = True
        signal.realized_outcome_name = "post_20d_return"
        signal.realized_outcome_value = -.1234
        signal.outcome_observation_date = date(2026, 9, 28)
        db.commit()
        row = next(r for r in get_upcoming_lockups(db) if r["lockup_id"] == signaled_id)
        assert row["realized_outcome_name"] == "post_20d_return"
        assert row["post_event_20d_return"] == -.1234
        assert row["outcome_observation_date"] == date(2026, 9, 28)
        assert row["result"] == "bearish_hit"
    finally:
        db.close()


def test_dashboard_separates_missed_t5_window_from_waiting_and_history():
    db, historical_id, pending_id, signaled_id = _dashboard_database()
    try:
        missed_id = _add_lockup(
            db, 5, CUTOFF,
            "unavailable", "observation_before_prospective_start")
        db.commit()

        rows = {row["lockup_id"]: row for row in get_upcoming_lockups(db)}
        assert rows[missed_id]["m8_status"] == "unavailable"
        assert rows[missed_id]["t5_timing_status"] == \
            "observation_before_prospective_start"
        assert rows[pending_id]["m8_status"] == "pending_observation"
        assert historical_id not in rows
        summary = get_research_summary(db)
        assert summary["missed_t5_window"] == 1
        assert summary["pending_observation"] == 1
        assert summary["historical_unavailable"] == 1
        assert summary["prospective_signals"] == 1
    finally:
        db.close()


def test_summary_counts_clean_cohort_separately_from_filtered_upcoming_rows():
    db, historical_id, pending_id, signaled_id = _dashboard_database()
    try:
        assert {row["lockup_id"] for row in get_upcoming_lockups(db)} == {
            pending_id, signaled_id}
        summary = get_research_summary(db)
        assert summary["eligible_lockups"] == 3
        assert summary["historical_unavailable"] == 1
        assert summary["pending_observation"] == 1
        assert summary["prospective_signals"] == 1
        assert (summary["historical_unavailable"] + summary["pending_observation"]
                + summary["prospective_signals"] == summary["eligible_lockups"])
    finally:
        db.close()


def test_summary_injected_date_matches_upcoming_lifecycle_projection():
    db, _historical_id, _pending_id, _signaled_id = _dashboard_database()
    try:
        lockup_id = _add_lockup(db, 6)
        db.commit()

        for today, expected in (
                (date(2026, 8, 23), "pending_observation"),
                (date(2026, 8, 24), "waiting_for_market_data"),
                (date(2026, 8, 25), "waiting_for_market_data")):
            upcoming = next(row for row in get_upcoming_lockups(db, today=today)
                            if row["lockup_id"] == lockup_id)
            summary = get_research_summary(db, today=today)

            assert upcoming["required_t5_date"] == date(2026, 8, 24)
            assert upcoming["m8_status"] == expected
            assert summary[expected] == 2
    finally:
        db.close()


def test_summary_resolves_today_once_outside_its_classification_loop():
    tree = ast.parse(inspect.getsource(get_research_summary))
    loops = [node for node in ast.walk(tree) if isinstance(node, ast.For)]

    assert len(loops) == 1
    assert not any(isinstance(node, ast.Call) and
                   isinstance(node.func, ast.Attribute) and
                   isinstance(node.func.value, ast.Name) and
                   node.func.value.id == "date" and node.func.attr == "today"
                   for node in ast.walk(loops[0]))


def test_summary_historical_count_is_computed_from_frozen_state_not_upcoming(monkeypatch):
    db, _historical_id, _pending_id, _signaled_id = _dashboard_database()
    try:
        monkeypatch.setattr(research_dashboard, "get_upcoming_lockups",
                            lambda db: (_ for _ in ()).throw(AssertionError(
                                "summary must classify the cohort explicitly")))

        summary = get_research_summary(db)

        assert summary["historical_unavailable"] == 1
        assert (summary["historical_unavailable"] + summary["missed_t5_window"] +
                summary["pending_observation"] + summary["prospective_signals"] ==
                summary["eligible_lockups"])
    finally:
        db.close()


def test_historical_reference_still_uses_frozen_discovery_rows(monkeypatch):
    captured = {}
    dataset = [
        {"lockup_id": 1, "observation_date": CUTOFF},
        {"lockup_id": 2, "observation_date": CUTOFF + timedelta(days=1)},
    ]
    monkeypatch.setattr(research_dashboard, "build_backtest_dataset", lambda db: dataset)

    def fake_analysis(rows, *args, **kwargs):
        captured["rows"] = rows
        return {"n_events": len(rows), "ols": {}, "groups": {}, "robustness": {
            "feature1_sign_stable": True, "feature2_sign_stable": True,
            "high_high_median_outcome_always_negative": True,
            "high_high_bearish_hit_rate_min": 1.0, "coefficient_summary": {}}}

    monkeypatch.setattr(research_dashboard, "analyze_two_feature_interaction", fake_analysis)
    report = research_dashboard.get_historical_reference(object())

    assert [row["lockup_id"] for row in captured["rows"]] == [1]
    assert report["discovery_sample_n"] == 1
    assert report["analysis_type"] == "historical_discovery"


def test_prospective_evaluation_remains_m8_prospective_only(monkeypatch):
    captured = {}

    def fake_evaluation(db, *, hypothesis_id):
        captured.update(db=db, hypothesis_id=hypothesis_id)
        return {"evaluation_mode": "prospective"}

    monkeypatch.setattr(research_dashboard, "evaluate_prospective_signals", fake_evaluation)
    sentinel_db = object()

    assert research_dashboard.get_prospective_evaluation(sentinel_db) == {
        "evaluation_mode": "prospective"}
    assert captured == {"db": sentinel_db, "hypothesis_id": HYPOTHESIS_ID}
