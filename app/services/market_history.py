import time
from dataclasses import asdict, dataclass
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Company, DailyPrice, IPO, Security, utc_now
from app.services.market_data.base import MarketDataProvider
from app.services.market_data.massive import MassiveMarketDataProvider
from app.services.market_summary import recompute_market_summary


@dataclass
class MarketIngestReport:
    securities_seen: int = 0
    securities_created: int = 0
    symbols_missing: int = 0
    bars_fetched: int = 0
    bars_created: int = 0
    bars_updated: int = 0
    provider_no_data: int = 0
    provider_errors: int = 0
    errors: int = 0
    skipped_current: int = 0

    def to_dict(self):
        return asdict(self)


def create_provider() -> MarketDataProvider:
    if settings.market_data_provider.lower() != "massive":
        raise ValueError(f"Unsupported MARKET_DATA_PROVIDER: {settings.market_data_provider}")
    return MassiveMarketDataProvider(settings.massive_api_key)


def initialize_primary_security(db: Session, company: Company) -> tuple[Security | None, bool]:
    ticker = (company.ticker or "").strip().upper()
    if not ticker:
        return None, False
    security = db.scalar(select(Security).where(
        Security.company_id == company.id, Security.ticker == ticker, Security.source == "sec_company"
    ))
    if security:
        return security, False
    security = Security(company_id=company.id, ticker=ticker, exchange=company.exchange,
                        is_primary=True, source="sec_company", provider_symbol=ticker)
    db.add(security)
    db.flush()
    return security, True


def ingest_market_history(db: Session, provider: MarketDataProvider, *, limit: int | None = None,
                          ipo_id: int | None = None, ticker: str | None = None, sleep_seconds: float = 0,
                          refresh: bool = False, refresh_days: int | None = None,
                          initial_lookback_days: int | None = None,
                          end_date: date | None = None) -> MarketIngestReport:
    report = MarketIngestReport()
    stmt = select(IPO, Company).join(Company, Company.id == IPO.company_id).order_by(
        (IPO.offering_status == "priced").desc(), IPO.first_filing_date.desc()
    )
    if ipo_id is not None:
        stmt = stmt.where(IPO.id == ipo_id)
    if ticker:
        stmt = stmt.where(func.upper(Company.ticker) == ticker.strip().upper())
    if limit:
        stmt = stmt.limit(limit)
    rows = db.execute(stmt).all()
    effective_end = end_date or (date.today() - timedelta(days=1))
    lookback_days = (initial_lookback_days if initial_lookback_days is not None
                     else settings.market_initial_lookback_days)
    recent_days = refresh_days if refresh_days is not None else settings.market_refresh_days
    if lookback_days < 1 or recent_days < 1:
        raise ValueError("market history lookback and refresh windows must be positive")
    requested = False
    for ipo, company in rows:
        report.securities_seen += 1
        security, created = initialize_primary_security(db, company)
        report.securities_created += int(created)
        if security is None:
            report.symbols_missing += 1
            continue
        # Security identity remains durable even when this provider is unavailable.
        if created:
            db.commit()
        latest = db.scalar(select(func.max(DailyPrice.trade_date)).where(
            DailyPrice.security_id == security.id, DailyPrice.provider == provider.name
        ))
        # A registration filing can precede trading by months or never lead to an
        # IPO, so it is deliberately not a market-history anchor.
        fallback = effective_end - timedelta(days=lookback_days - 1)
        initial_start = ipo.ipo_date or fallback
        if refresh:
            start = max(initial_start, effective_end - timedelta(days=recent_days - 1))
        else:
            start = initial_start if latest is None else latest + timedelta(days=1)
        if start > effective_end:
            report.skipped_current += 1
            # Summaries are derived state and may be stale even when the raw
            # history is current (for example, after ipo_price is enriched).
            recompute_market_summary(db, ipo, security, provider.name)
            db.commit()
            continue
        if requested and sleep_seconds > 0:
            time.sleep(sleep_seconds)
        requested = True
        try:
            bars = provider.get_daily_history(security.provider_symbol or security.ticker, start, effective_end)
        except Exception:
            report.provider_errors += 1
            report.errors += 1
            db.rollback()
            continue
        report.bars_fetched += len(bars)
        if not bars:
            report.provider_no_data += 1
            continue
        for bar in bars:
            stored = db.scalar(select(DailyPrice).where(
                DailyPrice.security_id == security.id, DailyPrice.trade_date == bar.trade_date,
                DailyPrice.provider == provider.name
            ))
            values = dict(open=bar.open, high=bar.high, low=bar.low, close=bar.close, volume=bar.volume,
                          adjusted_close=bar.adjusted_close, provider_symbol=security.provider_symbol or security.ticker,
                          fetched_at=utc_now())
            if stored is None:
                db.add(DailyPrice(security_id=security.id, trade_date=bar.trade_date, provider=provider.name, **values))
                report.bars_created += 1
            elif refresh:
                for key, value in values.items(): setattr(stored, key, value)
                report.bars_updated += 1
        db.flush()
        recompute_market_summary(db, ipo, security, provider.name)
        db.commit()
    return report
