from dataclasses import asdict, dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import DailyPrice, IPO, IPOMarketSummary, Security


@dataclass
class MarketSummaryRecomputeReport:
    ipos_seen: int = 0
    summaries_recomputed: int = 0
    no_stored_bars: int = 0

    def to_dict(self):
        return asdict(self)


def recompute_market_summary(db: Session, ipo: IPO, security: Security, provider: str) -> IPOMarketSummary | None:
    bars = db.scalars(select(DailyPrice).where(
        DailyPrice.security_id == security.id, DailyPrice.provider == provider
    ).order_by(DailyPrice.trade_date)).all()
    if not bars:
        return None
    first, latest = bars[0], bars[-1]
    high = max(Decimal(str(bar.high)) for bar in bars)
    ipo_price = Decimal(str(ipo.ipo_price)) if ipo.ipo_price is not None else None
    values = {
        "security_id": security.id, "as_of_date": latest.trade_date,
        "first_trade_date": first.trade_date, "first_day_open": first.open, "first_day_close": first.close,
        "latest_trade_date": latest.trade_date, "latest_close": latest.close, "post_ipo_high": high,
        "first_day_close_return_vs_ipo_price": ((Decimal(str(first.close)) - ipo_price) / ipo_price if ipo_price else None),
        "return_from_ipo_price": ((Decimal(str(latest.close)) - ipo_price) / ipo_price if ipo_price else None),
        "drawdown_from_post_ipo_high": (Decimal(str(latest.close)) - high) / high,
    }
    summary = db.scalar(select(IPOMarketSummary).where(IPOMarketSummary.ipo_id == ipo.id))
    if summary is None:
        summary = IPOMarketSummary(ipo_id=ipo.id, **values)
        db.add(summary)
    elif any(getattr(summary, key) != value for key, value in values.items()):
        for key, value in values.items():
            setattr(summary, key, value)
    return summary


def recompute_market_summaries(db: Session, provider: str, *, limit: int | None = None,
                               ipo_id: int | None = None,
                               ticker: str | None = None) -> MarketSummaryRecomputeReport:
    """Rebuild derived summaries using stored bars only.

    Unlike market-history ingestion, this function has no provider object and
    therefore cannot make a market-data request.
    """
    report = MarketSummaryRecomputeReport()
    stmt = select(IPO, Security).join(Security, Security.company_id == IPO.company_id).where(
        Security.is_primary.is_(True), Security.source == "sec_company"
    ).order_by(IPO.id, Security.id)
    if ipo_id is not None:
        stmt = stmt.where(IPO.id == ipo_id)
    if ticker:
        stmt = stmt.where(func.upper(Security.ticker) == ticker.strip().upper())
    if limit:
        stmt = stmt.limit(limit)

    for ipo, security in db.execute(stmt).all():
        report.ipos_seen += 1
        if recompute_market_summary(db, ipo, security, provider) is None:
            report.no_stored_bars += 1
        else:
            report.summaries_recomputed += 1
    db.commit()
    return report
