from datetime import date
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import Company, DailyPrice, IPO, IPOMarketSummary, Security
from app.services.market_data.base import DailyBar, MarketDataError, ProviderConfigurationError
from app.services.market_data.massive import MassiveMarketDataProvider
from app.services.market_history import ingest_market_history
from app.services.market_summary import recompute_market_summaries
from app.services.schema_upgrade import upgrade_milestone_5


def database():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def add_ipo(db, *, ipo_date=date(2025, 1, 2), filing_date=date(2020, 1, 1), price=Decimal("10")):
    company = Company(cik="0000000042", name="Market Co", ticker="mkt", exchange="NYSE")
    ipo = IPO(company=company, ipo_date=ipo_date, first_filing_date=filing_date,
              ipo_price=price, offering_status="priced")
    db.add(ipo)
    db.commit()
    return ipo


def add_filter_ipo(db, number, *, ticker=None, classification_status="unclassified",
                   candidate_type="unknown", offering_status="filed", primary_lockup_id=None,
                   primary_lockup_expiration_date=None, filing_date=None):
    company = Company(cik=f"{number:010d}", name=f"Market Co {number}",
                      ticker=ticker if ticker is not None else f"M{number}", exchange="NYSE")
    ipo = IPO(
        company=company, ipo_date=date(2025, 1, 2),
        first_filing_date=filing_date or date(2020, 1, number), ipo_price=Decimal("10"),
        classification_status=classification_status, candidate_type=candidate_type,
        offering_status=offering_status, primary_lockup_id=primary_lockup_id,
        primary_lockup_expiration_date=primary_lockup_expiration_date,
    )
    db.add(ipo)
    db.commit()
    return ipo


class Provider:
    name = "fake"

    def __init__(self, bars):
        self.bars = bars
        self.calls = []

    def get_daily_history(self, symbol, start_date, end_date):
        self.calls.append((symbol, start_date, end_date))
        return [bar for bar in self.bars if start_date <= bar.trade_date <= end_date]


def bar(day, close="11", high="12"):
    return DailyBar(day, Decimal("10"), Decimal(high), Decimal("9"), Decimal(close), 1234)


def test_massive_normalizes_response_and_requests_raw_bars():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"results": [{
            "t": 1735862400000, "o": 10.1, "h": 12, "l": 9, "c": 11.5, "v": 123,
        }]})

    provider = MassiveMarketDataProvider("secret", client=httpx.Client(transport=httpx.MockTransport(handler)))
    result = provider.get_daily_history("A/B", date(2025, 1, 1), date(2025, 1, 3))

    assert result == [DailyBar(date(2025, 1, 3), Decimal("10.1"), Decimal("12"),
                               Decimal("9"), Decimal("11.5"), 123, None)]
    assert seen[0].url.params["adjusted"] == "false"
    assert "/ticker/A%2FB/" in str(seen[0].url)


def test_massive_retries_429_and_transient_failures():
    statuses = iter((429, 503, 200))
    sleeps = []

    def handler(_request):
        status = next(statuses)
        return httpx.Response(status, headers={"Retry-After": "2"} if status == 429 else {}, json={})

    provider = MassiveMarketDataProvider("key", client=httpx.Client(transport=httpx.MockTransport(handler)),
                                         max_retries=2, backoff_seconds=.5, sleep_fn=sleeps.append)
    assert provider.get_daily_history("MKT", date(2025, 1, 1), date(2025, 1, 2)) == []
    assert sleeps == [2.0, 1.0]


def test_massive_retry_exhaustion_and_missing_key_are_clear():
    with pytest.raises(ProviderConfigurationError, match="MASSIVE_API_KEY is required"):
        MassiveMarketDataProvider(" ")
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(429)))
    provider = MassiveMarketDataProvider("key", client=client, max_retries=1, sleep_fn=lambda _: None)
    with pytest.raises(MarketDataError, match="after retries.*429"):
        provider.get_daily_history("MKT", date(2025, 1, 1), date(2025, 1, 2))


def test_ingest_initializes_security_creates_bars_and_is_idempotent():
    provider = Provider([bar(date(2025, 1, 3))])
    with Session(database()) as db:
        ipo = add_ipo(db)
        first = ingest_market_history(db, provider, end_date=date(2025, 1, 3))
        second = ingest_market_history(db, provider, end_date=date(2025, 1, 3))
        assert first.securities_created == first.bars_created == 1
        assert second.securities_created == second.bars_created == 0
        assert second.skipped_current == 1
        assert db.scalar(select(func.count()).select_from(Security)) == 1
        assert db.scalar(select(func.count()).select_from(DailyPrice)) == 1
        assert ipo.ipo_date == date(2025, 1, 2)
        assert ipo.market_summary.first_trade_date == date(2025, 1, 3)


@pytest.mark.parametrize(("argument", "value"), [
    ("classification_status", "classified"),
    ("candidate_type", "operating_company_ipo"),
    ("offering_status", "priced"),
])
def test_research_universe_value_filters(argument, value):
    provider = Provider([])
    with Session(database()) as db:
        matching = add_filter_ipo(db, 1, **{argument: value})
        add_filter_ipo(db, 2)

        report = ingest_market_history(
            db, provider, end_date=date(2025, 1, 3), **{argument: value}
        )

        assert report.securities_seen == 1
        assert provider.calls[0][0] == matching.company.ticker


def test_primary_lockup_filter_requires_selected_and_dated_lockup():
    provider = Provider([])
    with Session(database()) as db:
        add_filter_ipo(db, 1)
        add_filter_ipo(db, 2, primary_lockup_id=102)
        included = add_filter_ipo(
            db, 3, primary_lockup_id=103,
            primary_lockup_expiration_date=date(2025, 7, 1),
        )

        report = ingest_market_history(
            db, provider, primary_lockup_only=True, end_date=date(2025, 1, 3)
        )

        assert report.securities_seen == 1
        assert [call[0] for call in provider.calls] == [included.company.ticker]


def test_research_filters_compose_and_apply_before_limit():
    provider = Provider([])
    filters = dict(classification_status="classified", candidate_type="operating_company_ipo",
                   offering_status="priced", primary_lockup_only=True)
    with Session(database()) as db:
        # These sort ahead of the eligible row in the broad query. Applying the
        # limit first would therefore produce no eligible result.
        add_filter_ipo(db, 1, offering_status="priced", filing_date=date(2024, 1, 1))
        add_filter_ipo(db, 2, classification_status="classified", offering_status="priced",
                       filing_date=date(2023, 1, 1))
        eligible = add_filter_ipo(
            db, 3, classification_status="classified", candidate_type="operating_company_ipo",
            offering_status="priced", primary_lockup_id=103,
            primary_lockup_expiration_date=date(2025, 7, 1), filing_date=date(2020, 1, 1),
        )

        report = ingest_market_history(
            db, provider, limit=1, end_date=date(2025, 1, 3), **filters
        )

        assert report.securities_seen == 1
        assert [call[0] for call in provider.calls] == [eligible.company.ticker]


def test_existing_selection_modes_and_unfiltered_missing_ticker_behavior():
    with Session(database()) as db:
        first = add_filter_ipo(db, 1, ticker="ONE")
        second = add_filter_ipo(db, 2, ticker="TWO")
        add_filter_ipo(db, 3, ticker="")

        ticker_provider = Provider([])
        ticker_report = ingest_market_history(
            db, ticker_provider, ticker="one", end_date=date(2025, 1, 3)
        )
        id_provider = Provider([])
        id_report = ingest_market_history(
            db, id_provider, ipo_id=second.id, end_date=date(2025, 1, 3)
        )
        unfiltered_provider = Provider([])
        unfiltered_report = ingest_market_history(
            db, unfiltered_provider, end_date=date(2025, 1, 3)
        )

        assert ticker_report.securities_seen == 1
        assert ticker_provider.calls[0][0] == first.company.ticker
        assert id_report.securities_seen == 1
        assert id_provider.calls[0][0] == second.company.ticker
        assert unfiltered_report.securities_seen == 3
        assert unfiltered_report.symbols_missing == 1


def test_no_fetch_rerun_recomputes_summary_after_ipo_price_change():
    provider = Provider([bar(date(2025, 1, 3))])
    with Session(database()) as db:
        ipo = add_ipo(db, price=None)
        ingest_market_history(db, provider, end_date=date(2025, 1, 3))
        prices_before = db.execute(select(
            DailyPrice.id, DailyPrice.trade_date, DailyPrice.open, DailyPrice.high,
            DailyPrice.low, DailyPrice.close, DailyPrice.volume, DailyPrice.fetched_at,
        )).all()
        ipo.ipo_price = Decimal("10")
        db.commit()
        calls_before = len(provider.calls)

        report = ingest_market_history(db, provider, end_date=date(2025, 1, 3))

        assert report.skipped_current == 1
        assert len(provider.calls) == calls_before
        assert db.execute(select(
            DailyPrice.id, DailyPrice.trade_date, DailyPrice.open, DailyPrice.high,
            DailyPrice.low, DailyPrice.close, DailyPrice.volume, DailyPrice.fetched_at,
        )).all() == prices_before
        assert ipo.market_summary.first_day_close_return_vs_ipo_price == Decimal(".1")
        assert ipo.market_summary.return_from_ipo_price == Decimal(".1")


def test_offline_summary_recompute_is_filtered_and_idempotent():
    provider = Provider([bar(date(2025, 1, 3))])
    with Session(database()) as db:
        ipo = add_ipo(db, price=None)
        ingest_market_history(db, provider, end_date=date(2025, 1, 3))
        ipo.ipo_price = Decimal("10")
        db.commit()
        prices_before = db.execute(select(DailyPrice.id, DailyPrice.fetched_at)).all()

        first = recompute_market_summaries(db, "fake", ipo_id=ipo.id, ticker="mkt", limit=1)
        second = recompute_market_summaries(db, "fake", ipo_id=ipo.id, ticker="MKT", limit=1)

        assert first.summaries_recomputed == second.summaries_recomputed == 1
        assert provider.calls == [("MKT", date(2025, 1, 2), date(2025, 1, 3))]
        assert db.execute(select(DailyPrice.id, DailyPrice.fetched_at)).all() == prices_before
        assert ipo.market_summary.return_from_ipo_price == Decimal(".1")


def test_incremental_fetch_begins_after_latest_bar():
    provider = Provider([bar(date(2025, 1, 3)), bar(date(2025, 1, 4))])
    with Session(database()) as db:
        add_ipo(db)
        ingest_market_history(db, provider, end_date=date(2025, 1, 3))
        ingest_market_history(db, provider, end_date=date(2025, 1, 4))
        assert provider.calls[-1][1] == date(2025, 1, 4)
        assert db.scalar(select(func.count()).select_from(DailyPrice)) == 2


def test_missing_ipo_date_uses_bounded_lookback_not_first_filing_date():
    provider = Provider([])
    with Session(database()) as db:
        add_ipo(db, ipo_date=None, filing_date=date(2010, 2, 3))
        report = ingest_market_history(db, provider, end_date=date(2025, 1, 31), initial_lookback_days=10)
        assert provider.calls == [("MKT", date(2025, 1, 22), date(2025, 1, 31))]
        assert report.provider_no_data == 1


def test_refresh_only_upserts_configured_recent_window():
    provider = Provider([bar(date(2025, 1, 2)), bar(date(2025, 1, 30), close="13")])
    with Session(database()) as db:
        add_ipo(db)
        ingest_market_history(db, provider, end_date=date(2025, 1, 30))
        provider.bars = [bar(date(2025, 1, 30), close="14")]
        report = ingest_market_history(db, provider, end_date=date(2025, 1, 31), refresh=True, refresh_days=7)
        assert provider.calls[-1][1] == date(2025, 1, 25)
        assert report.bars_updated == 1 and report.bars_created == 0
        assert db.scalar(select(DailyPrice.close).where(DailyPrice.trade_date == date(2025, 1, 30))) == Decimal("14")
        assert db.scalar(select(func.count()).select_from(DailyPrice)) == 2


def test_market_summary_formulas_and_missing_ipo_price():
    provider = Provider([bar(date(2025, 1, 2), close="12", high="13"),
                         bar(date(2025, 1, 3), close="11", high="15")])
    with Session(database()) as db:
        ipo = add_ipo(db)
        ingest_market_history(db, provider, end_date=date(2025, 1, 3))
        summary = ipo.market_summary
        assert summary.first_day_close_return_vs_ipo_price == Decimal(".2")
        assert summary.return_from_ipo_price == Decimal(".1")
        expected_drawdown = (Decimal("-4") / Decimal("15")).quantize(Decimal("0.0000000001"))
        assert summary.drawdown_from_post_ipo_high == expected_drawdown

    provider = Provider([bar(date(2025, 1, 2))])
    with Session(database()) as db:
        ipo = add_ipo(db, price=None)
        ingest_market_history(db, provider, end_date=date(2025, 1, 2))
        assert ipo.market_summary.first_day_close_return_vs_ipo_price is None
        assert ipo.market_summary.return_from_ipo_price is None
        assert ipo.market_summary.drawdown_from_post_ipo_high is not None


def test_milestone_5_schema_upgrade_is_idempotent():
    engine = database()
    IPOMarketSummary.__table__.drop(engine)
    DailyPrice.__table__.drop(engine)
    Security.__table__.drop(engine)
    assert upgrade_milestone_5(engine) == ["securities", "daily_prices", "ipo_market_summary"]
    assert upgrade_milestone_5(engine) == []


def test_market_api_serializes_summary_and_prices():
    engine = database()
    with Session(engine) as db:
        ipo = add_ipo(db)
        ingest_market_history(db, Provider([bar(date(2025, 1, 3))]), end_date=date(2025, 1, 3))
        ipo_id = ipo.id

    def override_db():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    try:
        client = TestClient(app)
        detail = client.get(f"/api/ipos/{ipo_id}")
        prices = client.get(f"/api/ipos/{ipo_id}/prices")
        assert detail.status_code == prices.status_code == 200
        assert detail.json()["market_summary"]["first_trade_date"] == "2025-01-03"
        assert prices.json()[0] == {"date": "2025-01-03", "open": 10.0, "high": 12.0,
                                    "low": 9.0, "close": 11.0, "volume": 1234, "provider": "fake"}
    finally:
        app.dependency_overrides.clear()
