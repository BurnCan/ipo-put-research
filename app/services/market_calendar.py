"""Canonical expected US-equities sessions, independent of stored price data.

This module answers *when* XNYS sessions occur.  It intentionally does not
query :class:`DailyPrice` or make any claim that a bar is available.
"""
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from importlib.metadata import version

import exchange_calendars

CALENDAR_ID = "XNYS"
CALENDAR_PROVIDER = "exchange_calendars"
# Keep the research horizon independent of exchange_calendars' moving default
# window.  This covers the project's historical data and prospective events.
CALENDAR_START = date(1990, 1, 1)
CALENDAR_END = date(2035, 12, 31)


class MarketCalendarDateOutOfBounds(ValueError):
    """Raised when a request falls outside the canonical research horizon."""


@dataclass(frozen=True)
class SessionResolution:
    requested_event_date: date
    event_session: date
    observation_offset: int | None
    observation_session: date | None
    calendar_id: str = CALENDAR_ID
    calendar_provider: str = CALENDAR_PROVIDER
    calendar_version: str = version(CALENDAR_PROVIDER)


@lru_cache(maxsize=1)
def _calendar():
    return exchange_calendars.get_calendar(
        CALENDAR_ID,
        start=CALENDAR_START,
        end=CALENDAR_END,
    )


def _day(value: date) -> date:
    if not isinstance(value, date):
        raise TypeError("market-calendar inputs must be datetime.date values")
    if not CALENDAR_START <= value <= CALENDAR_END:
        raise MarketCalendarDateOutOfBounds(
            f"requested date {value} is outside {CALENDAR_ID} calendar range "
            f"{CALENDAR_START} through {CALENDAR_END}"
        )
    return value


def is_session(day: date) -> bool:
    return bool(_calendar().is_session(_day(day)))


def session_on_or_after(day: date) -> date:
    return _calendar().date_to_session(_day(day), direction="next").date()


def session_on_or_before(day: date) -> date:
    return _calendar().date_to_session(_day(day), direction="previous").date()


def session_offset(session_day: date, offset: int) -> date:
    day = _day(session_day)
    if not is_session(day):
        raise ValueError(f"{day} is not an {CALENDAR_ID} session")
    return _calendar().session_offset(day, offset).date()


def resolve_event_session(event_date: date) -> SessionResolution:
    event_date = _day(event_date)
    return SessionResolution(event_date, session_on_or_after(event_date), None, None)


def resolve_observation_session(event_date: date, observation_offset: int) -> SessionResolution:
    event_date = _day(event_date)
    event_session = session_on_or_after(event_date)
    return SessionResolution(
        event_date, event_session, observation_offset,
        session_offset(event_session, observation_offset),
    )
