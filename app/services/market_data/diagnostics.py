"""Read-only canonical coverage and provider-attempt diagnostics."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select

from app.models import DailyPrice, MarketDataBackfillAttempt


@dataclass(frozen=True)
class DiagnosticRequest:
    """A named canonical window for one durable security."""

    key: object
    security_id: int
    required_sessions: tuple[date, ...]


def _summary(required, present, attempts, *, as_of):
    required = tuple(dict.fromkeys(required))
    present = set(present) & set(required)
    missing = [day for day in required if day not in present]
    latest = {}
    # Ordering is deterministic: later attempted_at wins, with id as tie-breaker.
    for attempt in sorted(attempts, key=lambda row: (row.attempted_at, row.id or 0)):
        for day in missing:
            if attempt.requested_start_date <= day <= attempt.requested_end_date:
                latest[day] = attempt.status
    future = [day for day in missing if day > as_of]
    known_no_data = [day for day in missing if day <= as_of and latest.get(day) == "no_data"]
    errors = [day for day in missing if day <= as_of and latest.get(day) == "error"]
    attempted = [day for day in missing if day <= as_of and latest.get(day) in ("success", "partial")]
    unattempted = [day for day in missing if day <= as_of and day not in latest]
    categories = sum(bool(values) for values in (future, known_no_data, errors,
                                                  attempted, unattempted))
    if not missing:
        status = "complete"
    elif len(future) == len(missing):
        status = "not_reached"
    elif categories > 1:
        status = "mixed_attempt_history"
    elif known_no_data:
        status = "known_no_data"
    elif errors:
        status = "provider_error"
    else:
        status = "unattempted_missing" if unattempted else "mixed_attempt_history"
    return {
        "status": status,
        "required_sessions": list(required), "present_sessions": sorted(present),
        "missing_sessions": missing, "not_reached_sessions": future,
        "known_no_data_sessions": known_no_data, "provider_error_sessions": errors,
        "attempted_missing_sessions": attempted,
        "unattempted_missing_sessions": unattempted,
        "required_count": len(required), "present_count": len(present),
        "missing_count": len(missing), "known_no_data_count": len(known_no_data),
        "provider_error_count": len(errors), "attempted_missing_count": len(attempted),
        "unattempted_missing_count": len(unattempted), "not_reached_count": len(future),
    }


def diagnose_market_data_windows(db, requests, *, provider: str, as_of: date | None = None):
    """Batch diagnostics without changing coverage, attempts, or provider state."""
    requests = list(requests)
    if not requests:
        return {}
    as_of = as_of or date.today()
    security_ids = {request.security_id for request in requests}
    starts = [min(request.required_sessions) for request in requests if request.required_sessions]
    ends = [max(request.required_sessions) for request in requests if request.required_sessions]
    prices = defaultdict(set)
    attempts = defaultdict(list)
    if starts:
        # Coverage is deliberately provider-independent.
        for security_id, trade_date in db.execute(select(
                DailyPrice.security_id, DailyPrice.trade_date).where(
                    DailyPrice.security_id.in_(security_ids),
                    DailyPrice.trade_date.between(min(starts), max(ends)))):
            prices[security_id].add(trade_date)
        # Attempt provenance is deliberately provider-specific.
        for row in db.scalars(select(MarketDataBackfillAttempt).where(
                MarketDataBackfillAttempt.security_id.in_(security_ids),
                MarketDataBackfillAttempt.provider == provider,
                MarketDataBackfillAttempt.requested_end_date >= min(starts),
                MarketDataBackfillAttempt.requested_start_date <= max(ends))):
            attempts[row.security_id].append(row)
    return {request.key: _summary(request.required_sessions, prices[request.security_id],
                                  attempts[request.security_id], as_of=as_of)
            for request in requests}


def diagnose_market_data_window(db, security_id: int, provider: str,
                                required_sessions, *, as_of: date | None = None):
    """Convenient single-window form of :func:`diagnose_market_data_windows`."""
    request = DiagnosticRequest("window", security_id, tuple(required_sessions))
    return diagnose_market_data_windows(db, [request], provider=provider, as_of=as_of)["window"]
