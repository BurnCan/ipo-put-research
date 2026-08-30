"""Read-only readiness audit for the exact T-5 20-day signal window.

This measures the 21 canonical XNYS sessions needed to calculate ``return_20d``
and ``realized_vol_20d`` at T-5.  It is deliberately not an M6 v2 snapshot
status audit and never fetches, synthesizes, or writes market data.
"""
from collections import Counter
from datetime import date
from importlib.metadata import version

from sqlalchemy import func, select

from app.models import Company, IPO, IPOLockup, Security
from app.services.event_analysis.sessions import event_date_with_source
from app.services.market_calendar import (CALENDAR_ID, CALENDAR_PROVIDER,
                                          resolve_observation_session,
                                          session_offset)
from app.services.market_data.diagnostics import (DiagnosticRequest,
                                                  diagnose_market_data_windows)


def _readiness(diagnostic, observation_session, as_of):
    if diagnostic["missing_count"] == 0:
        return "complete"
    if observation_session > as_of:
        return "not_reached"
    if diagnostic["provider_error_count"]:
        return "provider_error"
    retryable = (diagnostic["unattempted_missing_count"] +
                 diagnostic["attempted_missing_count"])
    if retryable:
        return "backfill_candidate"
    if diagnostic["known_no_data_count"] == diagnostic["missing_count"]:
        return "provider_exhausted"
    return "mixed_incomplete"


def audit_t5_signal_readiness(db, *, provider, as_of_date=None,
                              classification_status="classified",
                              candidate_type="operating_company_ipo",
                              offering_status="priced", primary_lockup_only=True,
                              ticker=None, ipo_id=None, lockup_id=None, limit=None):
    """Return a deterministic JSON-ready report without mutating the session."""
    as_of = as_of_date or date.today()
    stmt = (select(IPO, Company, IPOLockup, Security)
            .join(Company, Company.id == IPO.company_id)
            .join(IPOLockup, IPOLockup.ipo_id == IPO.id)
            .join(Security, (Security.company_id == Company.id) & Security.is_primary))
    if classification_status is not None:
        stmt = stmt.where(IPO.classification_status == classification_status)
    if candidate_type is not None: stmt = stmt.where(IPO.candidate_type == candidate_type)
    if offering_status is not None: stmt = stmt.where(IPO.offering_status == offering_status)
    if primary_lockup_only: stmt = stmt.where(IPOLockup.id == IPO.primary_lockup_id)
    if ticker: stmt = stmt.where(func.upper(Security.ticker) == ticker.strip().upper())
    if ipo_id is not None: stmt = stmt.where(IPO.id == ipo_id)
    if lockup_id is not None: stmt = stmt.where(IPOLockup.id == lockup_id)
    stmt = stmt.order_by(Security.ticker, IPOLockup.id, Security.id)
    if limit is not None: stmt = stmt.limit(limit)
    rows = db.execute(stmt).all()

    planned, skipped, requests = [], [], []
    for ipo, company, lockup, security in rows:
        event_date, event_source = event_date_with_source(lockup)
        if event_date is None:
            skipped.append({"ipo_id": ipo.id, "lockup_id": lockup.id,
                            "security_id": security.id, "ticker": security.ticker,
                            "readiness": "no_known_event_date"})
            continue
        resolution = resolve_observation_session(event_date, -5)
        required = tuple(session_offset(resolution.observation_session, offset)
                         for offset in range(-20, 1))
        key = (lockup.id, security.id)
        planned.append((key, ipo, company, lockup, security, event_date,
                        event_source, resolution, required))
        requests.append(DiagnosticRequest(key, security.id, required))
    diagnostics = diagnose_market_data_windows(
        db, requests, provider=provider, as_of=as_of)

    details = []
    for (key, ipo, company, lockup, security, event_date, event_source,
         resolution, required) in planned:
        diagnostic = diagnostics[key]
        readiness = _readiness(diagnostic, resolution.observation_session, as_of)
        retryable_dates = sorted(diagnostic["attempted_missing_sessions"] +
                                 diagnostic["unattempted_missing_sessions"])
        categories = {}
        for day in diagnostic["present_sessions"]: categories[day] = "present"
        for day in diagnostic["not_reached_sessions"]: categories[day] = "future_not_reached"
        for day in diagnostic["known_no_data_sessions"]: categories[day] = "known_no_data"
        for day in diagnostic["provider_error_sessions"]: categories[day] = "provider_error"
        for day in retryable_dates: categories[day] = "unattempted_retryable"
        future_anomaly = (resolution.observation_session <= as_of and
                          bool(diagnostic["not_reached_sessions"]))
        details.append({
            "ipo_id": ipo.id, "lockup_id": lockup.id, "security_id": security.id,
            "ticker": security.ticker, "company_name": company.name,
            "canonical_event_date": event_date, "event_date_source": event_source,
            "canonical_event_session": resolution.event_session,
            "t5_observation_session": resolution.observation_session,
            "required_session_start": required[0], "required_session_end": required[-1],
            "required_sessions": list(required), "required_session_count": len(required),
            "session_classifications": [
                {"session": day, "classification": categories[day]} for day in required],
            "present_sessions": diagnostic["present_sessions"],
            "present_session_count": diagnostic["present_count"],
            "missing_sessions": diagnostic["missing_sessions"],
            "missing_session_count": diagnostic["missing_count"],
            "known_no_data_dates": diagnostic["known_no_data_sessions"],
            "known_no_data_count": diagnostic["known_no_data_count"],
            "provider_error_dates": diagnostic["provider_error_sessions"],
            "provider_error_count": diagnostic["provider_error_count"],
            "unattempted_retryable_dates": retryable_dates,
            "unattempted_retryable_count": len(retryable_dates),
            "future_not_reached_dates": diagnostic["not_reached_sessions"],
            "future_not_reached_count": diagnostic["not_reached_count"],
            "reached_window_contains_future_session": future_anomaly,
            "readiness": readiness, "configured_provider": provider,
            "calendar_id": CALENDAR_ID, "calendar_provider": CALENDAR_PROVIDER,
            "calendar_version": version(CALENDAR_PROVIDER), "as_of_date": as_of,
        })
    counts = Counter(item["readiness"] for item in details)
    reached = [item for item in details if item["t5_observation_session"] <= as_of]
    summary = {
        "selected_lockups": len(rows), "audited_windows": len(details),
        "skipped_no_event_date": len(skipped), "reached_t5_windows": len(reached),
        "not_reached_t5_windows": counts["not_reached"],
        "complete_windows": counts["complete"],
        "incomplete_reached_windows": sum(x["readiness"] != "complete" for x in reached),
        "total_required_sessions": sum(x["required_session_count"] for x in details),
        "total_present_sessions": sum(x["present_session_count"] for x in details),
        "total_missing_sessions": sum(x["missing_session_count"] for x in details),
        "known_no_data_sessions": sum(x["known_no_data_count"] for x in details),
        "provider_error_sessions": sum(x["provider_error_count"] for x in details),
        "unattempted_retryable_sessions": sum(x["unattempted_retryable_count"] for x in details),
        "future_not_reached_sessions": sum(x["future_not_reached_count"] for x in details),
        "provider_exhausted_windows": counts["provider_exhausted"],
        "provider_error_windows": counts["provider_error"],
        "backfill_candidate_windows": counts["backfill_candidate"],
        "mixed_incomplete_windows": counts["mixed_incomplete"],
    }
    return {"audit": "t5_signal_readiness", "as_of_date": as_of,
            "configured_provider": provider,
            "calendar": {"id": CALENDAR_ID, "provider": CALENDAR_PROVIDER,
                         "version": version(CALENDAR_PROVIDER)},
            "summary": summary, "details": details + skipped}
