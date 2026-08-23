"""Deterministic, offline XNYS calendar contract tests."""
from datetime import date

import pytest

from app.services.market_calendar import (
    CALENDAR_END, CALENDAR_ID, CALENDAR_PROVIDER, CALENDAR_START,
    MarketCalendarDateOutOfBounds, is_session, resolve_event_session,
    resolve_observation_session, session_offset, session_on_or_after,
    session_on_or_before,
)


def test_calendar_identity_and_normal_session():
    result = resolve_event_session(date(2024, 3, 6))
    assert is_session(date(2024, 3, 6))
    assert result.event_session == date(2024, 3, 6)
    assert result.calendar_id == CALENDAR_ID == "XNYS"
    assert result.calendar_provider == CALENDAR_PROVIDER == "exchange_calendars"
    assert result.calendar_version


def test_calendar_has_fixed_research_horizon():
    assert CALENDAR_START == date(1990, 1, 1)
    assert CALENDAR_END == date(2035, 12, 31)


@pytest.mark.parametrize("event_date", [date(2026, 8, 24), date(2027, 8, 23)])
def test_calendar_covers_current_and_future_m8_dates(event_date):
    assert resolve_event_session(event_date).event_session >= event_date


@pytest.mark.parametrize("event_date", [date(1989, 12, 31), date(2036, 1, 1)])
def test_date_outside_fixed_research_horizon_raises_clear_error(event_date):
    with pytest.raises(MarketCalendarDateOutOfBounds) as exc_info:
        resolve_event_session(event_date)

    message = str(exc_info.value)
    assert str(event_date) in message
    assert CALENDAR_ID in message
    assert str(CALENDAR_START) in message
    assert str(CALENDAR_END) in message


@pytest.mark.parametrize(("event_date", "session"), [
    (date(2024, 3, 9), date(2024, 3, 11)),
    (date(2024, 3, 10), date(2024, 3, 11)),
    (date(2024, 1, 15), date(2024, 1, 16)),
    (date(2024, 3, 29), date(2024, 4, 1)),
    (date(2024, 7, 4), date(2024, 7, 5)),
    (date(2024, 11, 28), date(2024, 11, 29)),
    (date(2024, 12, 25), date(2024, 12, 26)),
    (date(2025, 1, 1), date(2025, 1, 2)),
])
def test_non_session_event_aligns_forward(event_date, session):
    assert resolve_event_session(event_date).event_session == session
    assert session_on_or_after(event_date) == session


def test_session_boundaries_and_invalid_offset_origin():
    assert session_on_or_before(date(2024, 3, 10)) == date(2024, 3, 8)
    assert session_offset(date(2024, 3, 11), -1) == date(2024, 3, 8)
    with pytest.raises(ValueError):
        session_offset(date(2024, 3, 10), -1)


@pytest.mark.parametrize(("event_date", "observation"), [
    (date(2024, 3, 11), date(2024, 3, 4)),
    (date(2024, 7, 8), date(2024, 6, 28)),
    (date(2025, 1, 3), date(2024, 12, 26)),
])
def test_exact_fifth_prior_exchange_session(event_date, observation):
    result = resolve_observation_session(event_date, -5)
    assert result.observation_session == observation
    assert result.observation_offset == -5
