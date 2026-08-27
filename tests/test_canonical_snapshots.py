"""Offline regression coverage for the parallel M6 v2 lineage."""
from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models import LockupSignalSnapshot
from app.services.event_analysis.analysis import recompute_lockup_analysis
from app.services.event_analysis.constants import SNAPSHOT_VERSION_V1, SNAPSHOT_VERSION_V2
from app.services.event_analysis.lockup_snapshots import compute_canonical_snapshot
from app.services.market_calendar import (CALENDAR_ID, resolve_observation_session,
                                          session_offset, sessions_in_range)
from tests.test_event_analysis import analysis_database, bar


def inputs(event=date(2026, 6, 13), offset=-5):
    resolution = resolve_observation_session(event, offset)
    ipo = SimpleNamespace(ipo_date=date(2025, 1, 2), ipo_price=10, primary_shares=80,
                          secondary_shares=20, shares_offered=100, deal_size=1000)
    lockup = SimpleNamespace(duration_days=180, holder_group="all", lockup_type="standard",
                             confidence=.9)
    return resolution, ipo, lockup


def canonical_bars(end, count=41):
    dates = sessions_in_range(session_offset(end, -(count - 1)), end)
    return {day: bar(day, 10 + index / 10, high=11 + index / 10,
                     low=9 + index / 10, volume=100 + index)
            for index, day in enumerate(dates)}


def calculate(bars):
    resolution, ipo, lockup = inputs()
    return compute_canonical_snapshot(
        bars, ipo, lockup, observation_offset=-5, event_date=resolution.requested_event_date,
        event_date_source="stated", resolution=resolution)


def test_non_session_event_and_sparse_rows_do_not_move_observation_identity():
    resolution, _, _ = inputs()
    assert resolution.event_session == date(2026, 6, 15)
    assert resolution.observation_session == session_offset(date(2026, 6, 15), -5)
    bars = canonical_bars(resolution.observation_session)
    bars.pop(next(iter(bars)))
    assert calculate(bars)["observation_date"] == resolution.observation_session


def test_sparse_stored_rows_cannot_manufacture_twenty_session_features():
    resolution, _, _ = inputs()
    bars = canonical_bars(resolution.observation_session, 21)
    required = sessions_in_range(session_offset(resolution.observation_session, -20),
                                 resolution.observation_session)
    bars.pop(required[10])
    older = session_offset(required[0], -1)
    bars[older] = bar(older, 9)
    assert len(bars) == 21
    result = calculate(bars)
    assert result["return_20d"] is None
    assert result["realized_vol_20d"] is None


def test_feature_specific_partial_and_complete_snapshots():
    resolution, _, _ = inputs()
    partial = calculate(canonical_bars(resolution.observation_session, 21))
    assert partial["return_20d"] is not None
    assert partial["realized_vol_20d"] is not None
    assert partial["return_40d"] is None
    assert partial["realized_vol_40d"] is None
    assert partial["snapshot_status"] == "partial"
    complete = calculate(canonical_bars(resolution.observation_session))
    assert complete["snapshot_status"] == "complete"
    assert complete["missing_history_sessions"] == 0
    assert complete["calendar_id"] == CALENDAR_ID
    assert complete["return_40d"] == pytest.approx(14 / 10 - 1)


def test_missing_observation_bar_is_explicit_and_never_substituted():
    resolution, _, _ = inputs()
    bars = canonical_bars(resolution.observation_session)
    bars.pop(resolution.observation_session)
    result = calculate(bars)
    assert result["observation_date"] == resolution.observation_session
    assert result["snapshot_status"] == "unavailable"
    assert result["unavailable_reason"] == "missing_observation_bar"
    assert result["close"] is None


def test_v1_v2_coexist_and_v2_is_idempotent():
    resolution, _, _ = inputs()
    dates = list(canonical_bars(resolution.event_session, 80))
    db, lockup, security = analysis_database(resolution.requested_event_date, dates)
    try:
        recompute_lockup_analysis(db, lockup, security, snapshot_version=SNAPSHOT_VERSION_V1)
        v1 = db.scalar(select(LockupSignalSnapshot).where(
            LockupSignalSnapshot.snapshot_version == SNAPSHOT_VERSION_V1,
            LockupSignalSnapshot.observation_offset == -5))
        original = (v1.id, v1.observation_date, v1.close)
        recompute_lockup_analysis(db, lockup, security, snapshot_version=SNAPSHOT_VERSION_V2)
        recompute_lockup_analysis(db, lockup, security, snapshot_version=SNAPSHOT_VERSION_V2)
        rows = list(db.scalars(select(LockupSignalSnapshot).where(
            LockupSignalSnapshot.observation_offset == -5)))
        assert {row.snapshot_version for row in rows} == {"1", "2"}
        assert len([row for row in rows if row.snapshot_version == "2"]) == 1
        db.refresh(v1)
        assert (v1.id, v1.observation_date, v1.close) == original
    finally:
        db.close()
