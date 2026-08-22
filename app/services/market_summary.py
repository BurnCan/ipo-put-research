from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DailyPrice, IPO, IPOMarketSummary, Security


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
