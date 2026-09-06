from datetime import UTC, date, datetime

import httpx

from app.services.market_data.options_feasibility import (
    MassiveOptionsProbe, classify_feasibility, classify_http_failure, redact_url,
    summarize_quote_fields,
)


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _timestamp(day):
    return int(datetime.combine(day, datetime.min.time(), tzinfo=UTC).timestamp() * 1_000_000_000)


def test_redact_url_removes_secrets_without_hiding_request_shape():
    redacted = redact_url(
        "https://api.massive.com/v3/quotes/O:ABC?apiKey=very-secret&timestamp=2024-01-02&token=x"
    )
    assert "very-secret" not in redacted
    assert "token=x" not in redacted
    assert "apiKey=%5BREDACTED%5D" in redacted
    assert "timestamp=2024-01-02" in redacted


def test_401_and_403_have_distinct_access_categories():
    assert classify_http_failure(401) == "authentication_failure"
    assert classify_http_failure(403) == "entitlement_plan_restriction"


def test_provider_never_places_api_key_in_path_or_error_note():
    def denied(request):
        assert request.url.params["apiKey"] == "secret-value"
        return httpx.Response(403, json={"error": "not entitled"})

    result = MassiveOptionsProbe("secret-value", client=_client(denied)).discover_put_contracts(
        "TEST", date(2024, 1, 2))
    assert result.error_category == "entitlement_plan_restriction"
    assert "secret-value" not in (result.note or "")


def test_current_or_wrong_date_quote_is_not_accepted_as_historical():
    requested = date(2024, 1, 2)

    def response(_request):
        return httpx.Response(200, json={"results": [{
            "sip_timestamp": _timestamp(date(2026, 1, 2)), "bid_price": 1, "ask_price": 2,
        }]})

    result = MassiveOptionsProbe("key", client=_client(response)).historical_quotes(
        "O:TEST", requested)
    assert result.status == "no_quotes"
    assert result.error_category == "no_historical_data"


def test_historical_quote_requires_observed_timestamp_but_accepts_requested_day():
    requested = date(2024, 1, 2)

    def response(_request):
        return httpx.Response(200, json={"results": [{
            "sip_timestamp": _timestamp(requested), "bid_price": 1, "ask_price": 2,
        }]})

    result = MassiveOptionsProbe("key", client=_client(response)).historical_quotes(
        "O:TEST", requested)
    assert result.status == "ok"
    assert summarize_quote_fields(result.results)["quote_field_status"] == "complete_bid_ask"


def test_malformed_and_empty_provider_responses_are_distinct():
    malformed = MassiveOptionsProbe("key", client=_client(
        lambda _request: httpx.Response(200, json={"results": {}})))
    result = malformed.discover_put_contracts("TEST", date(2024, 1, 2))
    assert result.error_category == "malformed_unexpected_response"

    empty = MassiveOptionsProbe("key", client=_client(
        lambda _request: httpx.Response(200, json={"results": []})))
    result = empty.discover_put_contracts("TEST", date(2024, 1, 2))
    assert result.status == "no_contracts"
    assert result.error_category == "no_option_contracts"


def test_partial_bid_ask_fields_are_not_reported_as_complete():
    summary = summarize_quote_fields(({
        "sip_timestamp": _timestamp(date(2024, 1, 2)), "bid_price": 1.25, "bid_size": 4,
    },))
    assert summary["historical_bid_available"] is True
    assert summary["historical_ask_available"] is False
    assert summary["quote_field_status"] == "partial_fields"
    assert summary["optional_fields"]["bid_size"] is True


def _name(**overrides):
    row = {
        "contract_discovery_status": "historical_contracts_found",
        "historical_bid_available": True,
        "historical_ask_available": True,
        "later_valuation_status": "historical_quotes_available",
        "current_chain_status": "not_attempted",
        "error_category": None,
    }
    row.update(overrides)
    return row


def test_historical_feasibility_summary_classifications():
    assert classify_feasibility([_name(), _name()]) == "historical_backtest_feasible"
    assert classify_feasibility([_name(), _name(historical_ask_available=False,
                                                  later_valuation_status="partially_available")]) == (
        "historical_backtest_partially_feasible")
    assert classify_feasibility([_name(contract_discovery_status="no_contracts",
                                        historical_bid_available=None,
                                        historical_ask_available=None,
                                        later_valuation_status="not_attempted",
                                        current_chain_status="current_chain_available")]) == (
        "prospective_collection_only")
    assert classify_feasibility([_name(error_category="entitlement_plan_restriction")]) == (
        "provider_entitlement_unknown")
    assert classify_feasibility([_name(contract_discovery_status="no_contracts",
                                        historical_bid_available=None,
                                        historical_ask_available=None,
                                        later_valuation_status="not_attempted")]) == "provider_not_suitable"
