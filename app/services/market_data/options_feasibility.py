"""Read-only Massive options-data feasibility audit.

This module deliberately keeps options observations out of the database.  An
``as_of`` contract-reference response and a date-bounded quote response are the
only evidence accepted as historical; snapshots and latest quotes never are.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import httpx
from sqlalchemy import func, select

from app.models import Company, IPO, IPOLockup, LockupProspectiveSignal, Security
from app.services.event_analysis.sessions import event_date_with_source
from app.services.market_calendar import resolve_observation_session, session_offset

ERROR_CATEGORIES = {
    "unsupported_endpoint", "authentication_failure", "entitlement_plan_restriction",
    "no_historical_data", "no_option_contracts", "no_quotes_for_requested_date",
    "malformed_unexpected_response", "transient_provider_network_error",
}
SENSITIVE_QUERY_KEYS = {"apikey", "api_key", "token", "access_token", "key", "secret"}
FEASIBILITY_CLASSIFICATIONS = {
    "historical_backtest_feasible", "historical_backtest_partially_feasible",
    "prospective_collection_only", "provider_entitlement_unknown", "provider_not_suitable",
}


def redact_url(value: str) -> str:
    """Redact credentials in a URL without obscuring useful request context."""
    parts = urlsplit(value)
    query = urlencode([
        (key, "[REDACTED]" if key.lower() in SENSITIVE_QUERY_KEYS else item)
        for key, item in parse_qsl(parts.query, keep_blank_values=True)
    ])
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def classify_http_failure(status_code: int, payload: Any = None) -> str:
    if status_code == 401:
        return "authentication_failure"
    if status_code == 403:
        return "entitlement_plan_restriction"
    if status_code in (404, 405):
        return "unsupported_endpoint"
    if status_code in MassiveOptionsProbe.transient_statuses:
        return "transient_provider_network_error"
    # Provider errors which cannot safely be interpreted are not evidence of
    # absent data or an absent capability.
    return "malformed_unexpected_response"


@dataclass(frozen=True)
class ProviderResult:
    status: str
    results: tuple[dict[str, Any], ...] = ()
    error_category: str | None = None
    http_status: int | None = None
    note: str | None = None


class MassiveOptionsProbe:
    """Minimal adapter for explicitly historical Massive options endpoints."""

    name = "massive"
    base_url = "https://api.massive.com"
    transient_statuses = {408, 429, 500, 502, 503, 504}

    def __init__(self, api_key: str, *, client: httpx.Client | None = None):
        if not api_key or not api_key.strip():
            raise ValueError("MASSIVE_API_KEY is required for the live options audit")
        self._api_key = api_key.strip()
        self.client = client or httpx.Client(timeout=30)

    def _get(self, path: str, params: dict[str, Any]) -> ProviderResult:
        safe_params = {**params, "apiKey": self._api_key}
        try:
            response = self.client.get(f"{self.base_url}{path}", params=safe_params)
        except httpx.RequestError as exc:
            return ProviderResult("error", error_category="transient_provider_network_error",
                                  note=f"{type(exc).__name__}: provider request failed")
        if response.status_code >= 400:
            category = classify_http_failure(response.status_code)
            return ProviderResult("error", error_category=category,
                                  http_status=response.status_code,
                                  note=f"provider returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except (ValueError, TypeError):
            return ProviderResult("error", error_category="malformed_unexpected_response",
                                  http_status=response.status_code, note="response was not JSON")
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            return ProviderResult("error", error_category="malformed_unexpected_response",
                                  http_status=response.status_code,
                                  note="response did not contain a results list")
        if any(not isinstance(item, dict) for item in payload["results"]):
            return ProviderResult("error", error_category="malformed_unexpected_response",
                                  http_status=response.status_code,
                                  note="results contained a non-object item")
        return ProviderResult("ok", tuple(payload["results"]), http_status=response.status_code)

    def discover_put_contracts(self, ticker: str, as_of: date, *,
                               expiration_on_or_after: date | None = None,
                               limit: int = 3) -> ProviderResult:
        params = {
            "underlying_ticker": ticker, "contract_type": "put", "as_of": as_of.isoformat(),
            "expired": "true", "limit": limit, "sort": "expiration_date", "order": "asc",
        }
        if expiration_on_or_after is not None:
            params["expiration_date.gte"] = expiration_on_or_after.isoformat()
        result = self._get("/v3/reference/options/contracts", params)
        if result.status == "ok" and not result.results:
            return ProviderResult("no_contracts", error_category="no_option_contracts",
                                  http_status=result.http_status,
                                  note="historical as_of lookup returned no put contracts")
        return result

    def historical_quotes(self, contract_symbol: str, session: date, *, limit: int = 1000) -> ProviderResult:
        result = self._get(f"/v3/quotes/{quote(contract_symbol, safe='')}", {
            "timestamp.gte": session.isoformat(),
            "timestamp.lt": (session + timedelta(days=1)).isoformat(),
            "sort": "timestamp", "order": "asc", "limit": limit,
        })
        if result.status != "ok":
            return result
        historical = tuple(item for item in result.results if _quote_is_on_date(item, session))
        if not historical:
            category = ("no_quotes_for_requested_date" if not result.results else "no_historical_data")
            note = ("date-bounded query returned no quotes" if not result.results else
                    "returned quotes were not timestamped on the requested historical date")
            return ProviderResult("no_quotes", error_category=category,
                                  http_status=result.http_status, note=note)
        return ProviderResult("ok", historical, http_status=result.http_status)

    def current_chain(self, ticker: str) -> ProviderResult:
        """Probe current availability, kept explicitly separate from history."""
        result = self._get(f"/v3/snapshot/options/{quote(ticker, safe='')}", {
            "contract_type": "put", "limit": 1,
        })
        if result.status == "ok" and not result.results:
            return ProviderResult("empty", error_category="no_option_contracts",
                                  http_status=result.http_status,
                                  note="current snapshot returned no put contracts")
        return result


def _timestamp_ns(item: dict[str, Any]) -> int | None:
    for field in ("sip_timestamp", "participant_timestamp", "timestamp"):
        value = item.get(field)
        if isinstance(value, (int, float)):
            return int(value)
    return None


def _quote_is_on_date(item: dict[str, Any], requested: date) -> bool:
    timestamp = _timestamp_ns(item)
    if timestamp is None:
        return False
    # Massive option quote timestamps are Unix nanoseconds.  UTC date equality
    # is a conservative necessary check, not an inference from request params.
    try:
        observed = datetime.fromtimestamp(timestamp / 1_000_000_000, tz=UTC).date()
    except (OverflowError, OSError, ValueError):
        return False
    return observed == requested


def summarize_quote_fields(quotes: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    bid = any(item.get("bid_price") is not None for item in quotes)
    ask = any(item.get("ask_price") is not None for item in quotes)
    optional = {field: any(item.get(field) is not None for item in quotes) for field in (
        "bid_size", "ask_size", "last_price", "volume", "open_interest",
        "implied_volatility", "greeks",
    )}
    return {"historical_bid_available": bid, "historical_ask_available": ask,
            "quote_field_status": "complete_bid_ask" if bid and ask else "partial_fields",
            "optional_fields": optional}


def classify_feasibility(names: list[dict[str, Any]]) -> str:
    if not names:
        return "provider_not_suitable"
    if any(item.get("error_category") in {
            "authentication_failure", "entitlement_plan_restriction"} for item in names):
        return "provider_entitlement_unknown"
    complete = [item for item in names if
                item.get("contract_discovery_status") == "historical_contracts_found" and
                item.get("historical_bid_available") is True and
                item.get("historical_ask_available") is True and
                item.get("later_valuation_status") == "historical_quotes_available"]
    partial = [item for item in names if item.get("contract_discovery_status") ==
               "historical_contracts_found" and (item.get("historical_bid_available") is True or
               item.get("historical_ask_available") is True or
               item.get("later_valuation_status") == "partially_available")]
    if len(complete) == len(names):
        return "historical_backtest_feasible"
    if complete or partial:
        return "historical_backtest_partially_feasible"
    if any(item.get("current_chain_status") == "current_chain_available" for item in names):
        return "prospective_collection_only"
    return "provider_not_suitable"


def select_sample(db, *, as_of_date: date, limit: int = 12,
                  tickers: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """Select a deterministic clean-cohort sample, alternating recent and old."""
    m8_exists = select(LockupProspectiveSignal.id).where(
        LockupProspectiveSignal.lockup_id == IPOLockup.id).exists()
    stmt = (select(IPO, Company, IPOLockup, Security, m8_exists.label("is_m8"))
            .join(Company, Company.id == IPO.company_id)
            .join(IPOLockup, IPOLockup.ipo_id == IPO.id)
            .join(Security, (Security.company_id == Company.id) & Security.is_primary)
            .where(IPO.classification_status == "classified",
                   IPO.candidate_type == "operating_company_ipo",
                   IPO.offering_status == "priced", IPOLockup.id == IPO.primary_lockup_id))
    if tickers:
        stmt = stmt.where(func.upper(Security.ticker).in_(tickers))
    rows = []
    for ipo, company, lockup, security, is_m8 in db.execute(stmt):
        event_date, source = event_date_with_source(lockup)
        if event_date is None:
            continue
        resolution = resolve_observation_session(event_date, -5)
        if resolution.observation_session <= as_of_date:
            rows.append({"ipo_id": ipo.id, "lockup_id": lockup.id,
                         "security_id": security.id, "ticker": security.ticker,
                         "provider_symbol": security.provider_symbol or security.ticker,
                         "company_name": company.name, "event_date": event_date,
                         "event_date_source": source, "event_session": resolution.event_session,
                         "t5_date": resolution.observation_session, "is_m8_name": bool(is_m8)})
    rows.sort(key=lambda item: (not item["is_m8_name"], item["event_date"], item["ticker"]))
    if tickers:
        return rows[:limit]
    chosen = rows[:1] if rows and rows[0]["is_m8_name"] else []
    pool = [item for item in rows if item not in chosen]
    pool.sort(key=lambda item: (item["event_date"], item["ticker"]))
    take_old = True
    while pool and len(chosen) < limit:
        chosen.append(pool.pop(0 if take_old else -1))
        take_old = not take_old
    return chosen


def audit_options_feasibility(db, probe: MassiveOptionsProbe, *, as_of_date: date,
                              limit: int = 12, tickers: tuple[str, ...] = ()) -> dict[str, Any]:
    sampled = select_sample(db, as_of_date=as_of_date, limit=limit, tickers=tickers)
    names = [_audit_name(probe, item, as_of_date) for item in sampled]
    classification = classify_feasibility(names)
    return {
        "provider": probe.name, "run_at": datetime.now(UTC).isoformat(),
        "as_of_date": as_of_date.isoformat(), "sample_size": len(names),
        "classification": classification,
        "historical_contract_discovery_supported": _aggregate_boolean(
            names, "contract_discovery_status", "historical_contracts_found"),
        "historical_bid_ask_supported": _aggregate_pair(names),
        "historical_followup_valuation_supported": _aggregate_boolean(
            names, "later_valuation_status", "historical_quotes_available"),
        "entitlement_blocked": classification == "provider_entitlement_unknown",
        "findings": [
            "Historical support is credited only for as_of contract discovery and date-stamped quotes.",
            "Current option snapshots are recorded separately and are never historical evidence.",
            "This small-sample result is a capability audit, not a coverage guarantee.",
        ],
        "names": names,
    }


def _aggregate_boolean(names, field, success):
    if not names:
        return None
    if all(item.get(field) == success for item in names):
        return True
    if any(item.get(field) == success for item in names):
        return None
    return False


def _aggregate_pair(names):
    if not names:
        return None
    complete = [item.get("historical_bid_available") is True and
                item.get("historical_ask_available") is True for item in names]
    return True if all(complete) else (None if any(complete) else False)


def _contract_metadata(item):
    return {field: item.get(field) for field in (
        "ticker", "underlying_ticker", "expiration_date", "strike_price",
        "contract_type", "exercise_style", "shares_per_contract", "primary_exchange",
    )}


def _audit_name(probe, sample, as_of):
    result = {**{key: value.isoformat() if isinstance(value, date) else value
                 for key, value in sample.items() if key != "provider_symbol"},
              "contract_discovery_status": "not_attempted", "put_contracts_found": 0,
              "contracts": [], "historical_quote_status": "not_attempted",
              "historical_bid_available": None, "historical_ask_available": None,
              "later_valuation_status": "not_attempted", "later_valuations": [],
              "current_chain_status": "not_attempted", "error_category": None, "notes": []}
    last_followup = session_offset(sample["event_session"], 20)
    discovered = probe.discover_put_contracts(
        sample["provider_symbol"], sample["t5_date"], expiration_on_or_after=last_followup)
    if discovered.status != "ok":
        result["contract_discovery_status"] = discovered.status
        result["error_category"] = discovered.error_category
        result["notes"].append(discovered.note)
        if discovered.error_category not in {"authentication_failure", "entitlement_plan_restriction"}:
            current = probe.current_chain(sample["provider_symbol"])
            result["current_chain_status"] = ("current_chain_available" if current.results else current.status)
            if current.note: result["notes"].append(f"current chain: {current.note}")
        return result
    result["contract_discovery_status"] = "historical_contracts_found"
    result["put_contracts_found"] = len(discovered.results)
    result["contracts"] = [_contract_metadata(item) for item in discovered.results]
    contract_symbol = discovered.results[0].get("ticker")
    if not isinstance(contract_symbol, str) or not contract_symbol:
        result["contract_discovery_status"] = "malformed_contract_metadata"
        result["error_category"] = "malformed_unexpected_response"
        result["notes"].append("historical contract lacked a contract ticker")
        return result
    quotes = probe.historical_quotes(contract_symbol, sample["t5_date"])
    if quotes.status != "ok":
        result["historical_quote_status"] = quotes.status
        result["error_category"] = quotes.error_category
        result["notes"].append(quotes.note)
    else:
        fields = summarize_quote_fields(quotes.results)
        result.update(fields)
        result["historical_quote_status"] = fields["quote_field_status"]
    targets = [("event", sample["event_session"])] + [
        (f"plus_{offset}", session_offset(sample["event_session"], offset))
        for offset in (1, 5, 10, 20)]
    for label, day in targets:
        if day > as_of:
            result["later_valuations"].append({"label": label, "date": day.isoformat(),
                                               "status": "not_reached"})
            continue
        followup = probe.historical_quotes(contract_symbol, day)
        entry = {"label": label, "date": day.isoformat(), "status": followup.status,
                 "bid_available": False, "ask_available": False,
                 "error_category": followup.error_category}
        if followup.status == "ok":
            values = summarize_quote_fields(followup.results)
            entry.update({"status": values["quote_field_status"],
                          "bid_available": values["historical_bid_available"],
                          "ask_available": values["historical_ask_available"]})
        result["later_valuations"].append(entry)
        if followup.error_category in {"authentication_failure", "entitlement_plan_restriction"}:
            result["error_category"] = followup.error_category
    reached = [item for item in result["later_valuations"] if item["status"] != "not_reached"]
    full = [item for item in reached if item["bid_available"] and item["ask_available"]]
    result["later_valuation_status"] = ("historical_quotes_available" if reached and len(full) == len(reached)
                                         else "partially_available" if full else "unavailable")
    return result
