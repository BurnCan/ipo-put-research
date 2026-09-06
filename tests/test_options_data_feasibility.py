from datetime import UTC, date, datetime

import httpx

from app.services.market_data.options_feasibility import (
    MassiveOptionsProbe, ProviderResult, _aggregate_pair, _audit_name,
    audit_options_feasibility, classify_feasibility,
    classify_http_failure, redact_url, summarize_quote_fields,
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
        "optionable_at_t5": True,
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
                                        optionable_at_t5=False,
                                        historical_bid_available=None,
                                        historical_ask_available=None,
                                        later_valuation_status="not_attempted",
                                        current_chain_status="current_chain_available")]) == (
        "cohort_not_optionable_at_t5")
    assert classify_feasibility([_name(error_category="entitlement_plan_restriction")]) == (
        "provider_entitlement_unknown")
    assert classify_feasibility([_name(contract_discovery_status="error",
                                        optionable_at_t5=None,
                                        historical_bid_available=None,
                                        historical_ask_available=None,
                                        later_valuation_status="not_attempted",
                                        error_category="unsupported_endpoint")]) == (
        "provider_not_suitable")
    assert classify_feasibility([_name(contract_discovery_status="no_contracts",
                                        optionable_at_t5=False,
                                        historical_bid_available=None,
                                        historical_ask_available=None,
                                        later_valuation_status="not_attempted")]) == (
        "cohort_not_optionable_at_t5")


class _NoContractsProbe:
    name = "fake"

    def __init__(self, discovery):
        self.discovery = discovery

    def discover_put_contracts(self, _ticker, _as_of, **_kwargs):
        return self.discovery

    def current_chain(self, _ticker):
        return ProviderResult("empty")


def _sample():
    return {
        "ticker": "TEST", "provider_symbol": "TEST", "t5_date": date(2024, 1, 8),
        "event_date": date(2024, 1, 16), "event_session": date(2024, 1, 16),
    }


def test_successful_empty_contract_lookup_means_not_optionable_not_provider_unsuitable():
    result = _audit_name(_NoContractsProbe(ProviderResult(
        "no_contracts", error_category="no_option_contracts", http_status=200)),
        _sample(), date(2024, 3, 31))

    assert result["optionable_at_t5"] is False
    assert classify_feasibility([result]) == "cohort_not_optionable_at_t5"
    assert _aggregate_pair([result]) is None


def test_transient_contract_failure_leaves_optionability_unknown():
    result = _audit_name(_NoContractsProbe(ProviderResult(
        "error", error_category="transient_provider_network_error", http_status=503)),
        _sample(), date(2024, 3, 31))

    assert result["optionable_at_t5"] is None
    assert classify_feasibility([result]) == "provider_capability_unknown"


def test_reference_success_and_non_optionable_cohort_state_can_coexist(monkeypatch):
    monkeypatch.setattr(
        "app.services.market_data.options_feasibility.select_sample",
        lambda *_args, **_kwargs: [_sample()],
    )
    report = audit_options_feasibility(
        None,
        _NoContractsProbe(ProviderResult(
            "no_contracts", error_category="no_option_contracts", http_status=200)),
        as_of_date=date(2024, 3, 31),
    )

    assert report["historical_contract_discovery_supported"] is True
    assert report["classification"] == "cohort_not_optionable_at_t5"
    assert report["historical_bid_ask_supported"] is None
    assert report["historical_followup_valuation_supported"] is None
    assert report["optionable_at_t5_count"] == 0
    assert report["not_optionable_at_t5_count"] == 1
    assert report["optionability_unknown_count"] == 0


def test_contract_discovery_uses_t5_liveness_not_plus20_survival():
    requested = date(2024, 1, 8)

    def response(request):
        assert request.url.params["as_of"] == requested.isoformat()
        assert request.url.params["expiration_date.gte"] == requested.isoformat()
        return httpx.Response(200, json={"results": []})

    MassiveOptionsProbe("key", client=_client(response)).discover_put_contracts(
        "TEST", requested, expiration_on_or_after=requested)


class _RecordingProbe:
    name = "fake"

    def __init__(self):
        self.discovery_arguments = None
        self.quote_dates = []

    def discover_put_contracts(self, ticker, as_of, **kwargs):
        self.discovery_arguments = (ticker, as_of, kwargs)
        # Deliberately return later expiration first. Selection must use stable
        # contract metadata, but must not prefer survival through +20.
        return ProviderResult("ok", ({
            "ticker": "O:TEST_LATER", "expiration_date": "2024-03-15",
            "strike_price": 10, "contract_type": "put",
        }, {
            "ticker": "O:TEST_EARLY", "expiration_date": "2024-01-19",
            "strike_price": 10, "contract_type": "put",
        }))

    def historical_quotes(self, symbol, day):
        self.quote_dates.append((symbol, day))
        return ProviderResult("ok", ({
            "sip_timestamp": _timestamp(day), "bid_price": 1, "ask_price": 2,
        },))


def test_expiring_contract_still_establishes_entry_capability_without_post_expiry_calls():
    probe = _RecordingProbe()
    sample = {
        "ticker": "TEST", "provider_symbol": "TEST", "t5_date": date(2024, 1, 8),
        "event_date": date(2024, 1, 16), "event_session": date(2024, 1, 16),
    }

    result = _audit_name(probe, sample, date(2024, 3, 31))

    assert probe.discovery_arguments[2]["expiration_on_or_after"] == sample["t5_date"]
    assert result["selected_contract_symbol"] == "O:TEST_EARLY"
    assert result["selected_contract_expiration_date"] == "2024-01-19"
    assert result["contracts"][0]["expiration_date"] == "2024-01-19"
    assert result["contract_expired_before_plus20"] is True
    assert result["historical_bid_available"] is True
    assert result["historical_ask_available"] is True
    assert classify_feasibility([result]) == "historical_backtest_feasible"

    expired = [item for item in result["later_valuations"]
               if item["status"] == "contract_expired"]
    assert {item["label"] for item in expired} == {"plus_5", "plus_10", "plus_20"}
    assert all(item["error_category"] is None for item in expired)
    assert all(day <= date(2024, 1, 19) for _symbol, day in probe.quote_dates)
    assert {symbol for symbol, _day in probe.quote_dates} == {"O:TEST_EARLY"}
